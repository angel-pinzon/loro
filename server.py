"""Servidor de Loro: el estado del juego, el WebSocket y las tres maquinas.

Una sola sala, todo en memoria. El bucle de rondas corre como tarea de fondo y
se comunica con los navegadores por WebSocket; el audio viaja por HTTP normal
(un .opus por frase) porque asi el navegador lo cachea y lo reproduce solo.
"""

import asyncio
import json
import random
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import oido
from oido import Oido, acierta, preparar_micro
from trivias import Trivias
from voz import Voz

RAIZ = Path(__file__).parent
MEDIA = RAIZ / "media"
CONFIG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))

# Frases fijas del loro. Se sintetizan una vez y salen de cache para siempre.
MULETILLAS = [
    "Let's see who is going to answer!",
    "Spin the wheel! Who is it?",
    "Ooh, the wheel is turning!",
]


class Jugador:
    def __init__(self, nombre):
        self.nombre = nombre
        self.estrellas = 0
        self.turnos = 0
        self.aciertos_escritos = 0
        self.respuesta = None
        self.conectado = True
        self.ws = None

    def resumen(self):
        return {
            "nombre": self.nombre,
            "estrellas": self.estrellas,
            "turnos": self.turnos,
            "aciertos_escritos": self.aciertos_escritos,
            "conectado": self.conectado,
            "respondio": self.respuesta is not None,
            "activo": self.estrellas < CONFIG["juego"]["estrellas_para_salir"],
        }


class Sala:
    def __init__(self):
        self.jugadores = {}
        # El profe mira y manda, pero no es un jugador: si entrara como uno mas
        # la ruleta podria elegirle a el para responder en ingles.
        self.observadores = set()
        self.fase = "lobby"
        self.trivia = None
        self.elegido = None
        self.revelacion = None
        self.veredicto = None
        self.ronda = 0
        self.bucle = None
        # Eventos con los que el bucle de rondas espera a los niños
        self.todos_respondieron = asyncio.Event()
        self.audio_recibido = asyncio.Event()
        self.avanzar = asyncio.Event()
        self.audio_micro = None
        self.siguiente_trivia = None

    def reinicia(self):
        """Deja la sala como recien abierta, conservando quien esta dentro.

        Sin esto la segunda partida arrancaba en 'Question number 4' y, peor,
        terminaba sola: todos seguian con sus cinco estrellas de la anterior.
        """
        self.ronda = 0
        self.trivia = None
        self.elegido = None
        self.revelacion = None
        self.veredicto = None
        self.audio_micro = None
        for jugador in self.jugadores.values():
            jugador.estrellas = 0
            jugador.turnos = 0
            jugador.aciertos_escritos = 0
            jugador.respuesta = None

    def activos(self):
        tope = CONFIG["juego"]["estrellas_para_salir"]
        return [j for j in self.jugadores.values() if j.estrellas < tope]

    def terminado(self):
        return bool(self.jugadores) and not self.activos()

    def estado(self):
        return {
            "tipo": "estado",
            "fase": self.fase,
            "ronda": self.ronda,
            "jugadores": [j.resumen() for j in self.jugadores.values()],
            "pregunta": self.trivia["pregunta"] if self.trivia else None,
            "pista": self.trivia["pista"] if self.trivia else None,
            "elegido": self.elegido,
            "revelacion": self.revelacion,
            "veredicto": self.veredicto,
            "tema": trivias.tema if trivias else CONFIG["juego"]["tema"],
            "edades": trivias.edades if trivias else CONFIG["juego"]["edades"],
            "estrellas_para_salir": CONFIG["juego"]["estrellas_para_salir"],
            "terminado": self.terminado(),
        }


sala = Sala()
voz = Voz(CONFIG)
oreja = Oido(CONFIG)
trivias = None


async def difunde(mensaje):
    muertos = []
    for jugador in sala.jugadores.values():
        if jugador.ws is None:
            continue
        try:
            await jugador.ws.send_json(mensaje)
        except Exception:
            muertos.append(jugador)
    for jugador in muertos:
        jugador.conectado = False
        jugador.ws = None

    for ws in list(sala.observadores):
        try:
            await ws.send_json(mensaje)
        except Exception:
            sala.observadores.discard(ws)


async def sincroniza():
    await difunde(sala.estado())


async def habla(texto, idioma="English", quien="loro", subtitular=True):
    """Sintetiza y manda a todos a reproducirlo. Bloquea hasta que lo reparte.

    'subtitular' es falso para la pregunta: ya sale escrita en grande en el
    centro, y repetirla debajo solo ensucia la pantalla.
    """
    nombre = await asyncio.to_thread(voz.decir, texto, idioma)
    await difunde({"tipo": "habla", "url": f"/media/{nombre}", "texto": texto,
                   "quien": quien, "subtitular": subtitular})
    return nombre


def elige_participante():
    """Ruleta ponderada: el que menos turnos lleva tiene mas papeletas.

    Con azar puro alguno sale tres veces seguidas y otro no sale nunca, que en
    una clase de 25 se nota y desmotiva. Sigue pareciendo una ruleta.
    """
    candidatos = sala.activos()
    if not candidatos:
        return None
    pesos = [1.0 / (j.turnos + 1) ** 2 for j in candidatos]
    return random.choices(candidatos, weights=pesos, k=1)[0]


async def espera(evento, segundos):
    """Espera un evento o a que el profe pulse 'siguiente'. True si llego a tiempo."""
    sala.avanzar.clear()
    tareas = [asyncio.create_task(evento.wait()), asyncio.create_task(sala.avanzar.wait())]
    hechas, pendientes = await asyncio.wait(
        tareas, timeout=segundos, return_when=asyncio.FIRST_COMPLETED)
    for tarea in pendientes:
        tarea.cancel()
    return evento.is_set()


async def prepara_trivia():
    sala.siguiente_trivia = await asyncio.to_thread(trivias.nueva)


async def hay_gente():
    """Si se cae toda la clase, el juego espera en vez de seguir gastando rondas."""
    while not any(j.conectado for j in sala.activos()):
        await asyncio.sleep(2)


async def ronda():
    juego = CONFIG["juego"]
    await hay_gente()

    if sala.siguiente_trivia is None:
        await prepara_trivia()
    sala.trivia = sala.siguiente_trivia
    sala.siguiente_trivia = None
    sala.ronda += 1
    sala.elegido = None
    sala.revelacion = None
    sala.veredicto = None
    for jugador in sala.jugadores.values():
        jugador.respuesta = None

    # --- 1. La pregunta, a ciegas -------------------------------------------
    sala.fase = "pregunta"
    sala.todos_respondieron.clear()
    await sincroniza()
    await habla(f"Question number {sala.ronda}. {sala.trivia['pregunta']}", subtitular=False)

    # La siguiente pregunta se va cocinando mientras esta se juega: asi el
    # segundo y medio de Gemini no cae nunca dentro del tiempo de nadie.
    cocina = asyncio.create_task(prepara_trivia())

    await espera(sala.todos_respondieron, juego["segundos_para_responder"])

    # --- 2. La ruleta, despues de bloquear las respuestas --------------------
    # Girar despues es lo que impide copiar: cuando escribes no sabes si te va
    # a tocar hablar, asi que todos tienen que comprometerse de verdad.
    sala.fase = "ruleta"
    elegido = elige_participante()
    if elegido is None:
        return
    sala.elegido = elegido.nombre
    elegido.turnos += 1
    await sincroniza()
    await habla(random.choice(MULETILLAS))
    await asyncio.sleep(2.5)

    # --- 3. El microfono ----------------------------------------------------
    sala.fase = "micro"
    sala.audio_micro = None
    sala.audio_recibido.clear()
    await sincroniza()
    llego = await espera(sala.audio_recibido, juego["segundos_de_microfono"])

    dicho, correcto = "", False
    if llego and sala.audio_micro:
        wav, clip = sala.audio_micro
        dicho = await asyncio.to_thread(oreja.transcribir, wav)
        correcto, motivo = acierta(
            dicho, sala.trivia["respuestas_aceptadas"], CONFIG["oido"]["parecido_minimo"])

        # La reaccion de Gemini se pide YA, y mientras la clase escucha al niño.
        # Ese replay tapa la espera en lugar de dejar tres segundos de silencio.
        pensando = asyncio.create_task(
            asyncio.to_thread(trivias.reaccion, sala.trivia, elegido.nombre, dicho, correcto))
        sala.veredicto = {"dicho": dicho, "correcto": correcto, "motivo": motivo}
        await sincroniza()
        await difunde({"tipo": "habla", "url": f"/media/{clip}",
                       "texto": dicho, "quien": elegido.nombre})
        await asyncio.sleep(0.5)
        reaccion = await pensando
    else:
        sala.veredicto = {"dicho": "", "correcto": False, "motivo": "no contesto a tiempo"}
        await sincroniza()
        reaccion = (f"No worries {elegido.nombre}! The answer is "
                    f"{sala.trivia['respuestas_aceptadas'][0]}. Let's keep going!")

    if correcto:
        elegido.estrellas += 1

    # --- 4. Se destapa todo -------------------------------------------------
    sala.fase = "revelacion"
    sala.revelacion = []
    for jugador in sala.jugadores.values():
        if jugador.respuesta is None:
            continue
        bien, _ = acierta(jugador.respuesta, sala.trivia["respuestas_aceptadas"],
                          CONFIG["oido"]["parecido_minimo"])
        if bien:
            jugador.aciertos_escritos += 1
        sala.revelacion.append(
            {"nombre": jugador.nombre, "texto": jugador.respuesta, "correcto": bien})
    await sincroniza()
    await habla(reaccion)

    if not cocina.done():
        await cocina
    sala.avanzar.clear()
    await espera(sala.avanzar, 4)


async def partida():
    try:
        while not sala.terminado():
            await ronda()
            if not sala.activos():
                break
        sala.fase = "final"
        await sincroniza()
        await habla("That is the end of the game! Everybody got five stars. Great job, class!")
    except asyncio.CancelledError:
        raise
    except Exception as error:  # que un fallo no deje la clase colgada en blanco
        sala.fase = "error"
        await difunde({"tipo": "error", "detalle": f"{type(error).__name__}: {error}"})
        raise


@asynccontextmanager
async def ciclo(app):
    global trivias
    MEDIA.mkdir(exist_ok=True)
    print("Cargando la voz...")
    voz.cargar()
    print("Cargando el oido...")
    oreja.cargar()
    trivias = Trivias(CONFIG)
    print(f"Loro listo en http://localhost:{CONFIG['servidor']['puerto']}")
    yield


app = FastAPI(lifespan=ciclo)
app.mount("/media", StaticFiles(directory=MEDIA), name="media")


@app.get("/")
async def raiz():
    return FileResponse(RAIZ / "static" / "index.html")


@app.post("/micro")
async def recibe_micro(nombre: str = Form(...), audio: UploadFile = None):
    """Llega el WebM del navegador del niño al que le toco el turno."""
    if sala.fase != "micro" or nombre != sala.elegido:
        return {"ok": False, "motivo": "no es tu turno"}

    bruto = MEDIA / f"micro_{uuid.uuid4().hex[:12]}.webm"
    bruto.write_bytes(await audio.read())
    wav, clip = await asyncio.to_thread(preparar_micro, bruto, MEDIA)
    sala.audio_micro = (wav, clip)
    sala.audio_recibido.set()
    return {"ok": True}


async def manda_profe(datos):
    accion = datos.get("accion")
    if accion == "empezar" and (sala.bucle is None or sala.bucle.done()):
        sala.reinicia()
        await sincroniza()
        sala.bucle = asyncio.create_task(partida())
    elif accion == "siguiente":
        sala.avanzar.set()
    elif accion == "configurar":
        trivias.configura(datos.get("tema"), datos.get("edades"))
        # La pregunta ya cocinada es del tema anterior: se tira, o la primera
        # ronda del nuevo tema saldria con la trivia vieja.
        sala.siguiente_trivia = None
        await sincroniza()

    elif accion == "estrella":
        jugador = sala.jugadores.get(datos.get("nombre"))
        if jugador:
            # Con tope: dos clics del profe no deben dejar a nadie con siete
            # estrellas de cinco ni descuadrar el recuento del final.
            tope = CONFIG["juego"]["estrellas_para_salir"]
            jugador.estrellas = min(tope, max(0, jugador.estrellas + int(datos.get("cuanto", 1))))
            await sincroniza()
    elif accion == "expulsar":
        sala.jugadores.pop(datos.get("nombre"), None)
        await sincroniza()


@app.websocket("/ws")
async def canal(ws: WebSocket):
    await ws.accept()
    jugador = None
    try:
        while True:
            datos = await ws.receive_json()
            tipo = datos.get("tipo")

            if tipo == "entrar" and datos.get("observador"):
                sala.observadores.add(ws)
                await ws.send_json(sala.estado())

            elif tipo == "entrar":
                nombre = (datos.get("nombre") or "").strip()[:20]
                if not nombre:
                    continue
                # Reconectar con el mismo nombre conserva las estrellas: a algun
                # niño se le va a caer el wifi y no puede perder lo ganado.
                jugador = sala.jugadores.setdefault(nombre, Jugador(nombre))
                jugador.conectado = True
                jugador.ws = ws
                await sincroniza()

            elif tipo == "respuesta" and jugador and sala.fase == "pregunta":
                jugador.respuesta = (datos.get("texto") or "").strip()[:120]
                if all(j.respuesta is not None for j in sala.activos() if j.conectado):
                    sala.todos_respondieron.set()
                await sincroniza()

            elif tipo == "profe":
                if datos.get("clave") == CONFIG["servidor"].get("clave_profe", "profe"):
                    await manda_profe(datos)

    except WebSocketDisconnect:
        pass
    finally:
        sala.observadores.discard(ws)
        if jugador:
            jugador.conectado = False
            jugador.ws = None
            try:
                await sincroniza()
            except Exception:
                pass
