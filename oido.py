"""Oido del loro: transcribe lo que dijo el niño y decide si acierta.

El reconocimiento falla justo donde mas duele: una palabra suelta sin contexto.
Medido, 'Cold' se oyo como 'Code' incluso con audio limpio y acento perfecto.
De ahi dos defensas: se pide la respuesta en frase completa (le da contexto al
reconocedor y ademas es mejor practica), y la comparacion es tolerante.
"""

import re
import subprocess
import threading
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import torch

ARTICULOS = {"a", "an", "the", "it", "is", "its", "they", "are", "this", "that"}


class Oido:
    def __init__(self, config):
        self.ajustes = config["oido"]
        self.asr = None
        self.turno = threading.Lock()

    def cargar(self):
        import logging
        import warnings

        warnings.filterwarnings("ignore")
        logging.getLogger("transformers").setLevel(logging.ERROR)
        from transformers import pipeline

        self.asr = pipeline(
            "automatic-speech-recognition",
            model=self.ajustes["modelo"],
            device=0,
            dtype=torch.float16,
        )

    def transcribir(self, ruta_wav):
        with self.turno:
            salida = self.asr(str(ruta_wav), generate_kwargs={"language": "english"})
        return (salida["text"] or "").strip()


def normaliza(texto):
    """Minusculas, sin acentos ni puntuacion: los niños escriben COLD, cold. y 'cold'."""
    plano = unicodedata.normalize("NFD", texto.lower())
    plano = "".join(c for c in plano if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", plano)).strip()


def acierta(dicho, aceptadas, parecido_minimo=0.85):
    """Compara con tolerancia. Devuelve (bool, motivo) para poder explicarlo.

    Tres pasadas, de la mas estricta a la mas indulgente:
    1. La respuesta aparece tal cual dentro de la frase ('it is cold' -> 'cold').
    2. Alguna palabra dicha se parece lo suficiente ('beautifull' -> 'beautiful').
    3. Nada encaja.
    """
    limpio = normaliza(dicho)
    if not limpio:
        return False, "no se oyo nada"

    palabras = [p for p in limpio.split() if p not in ARTICULOS]
    for esperada in aceptadas:
        objetivo = normaliza(esperada)
        if not objetivo:
            continue
        if re.search(rf"\b{re.escape(objetivo)}\b", limpio):
            return True, f"dijo '{objetivo}'"

        # La respuesta esperada puede ser de varias palabras: se compara entera
        # contra la frase, y palabra a palabra para las erratas sueltas.
        if SequenceMatcher(None, objetivo, limpio).ratio() >= parecido_minimo:
            return True, f"'{limpio}' se parece a '{objetivo}'"
        for palabra in palabras:
            if len(palabra) > 3 and SequenceMatcher(None, objetivo, palabra).ratio() >= parecido_minimo:
                return True, f"'{palabra}' se parece a '{objetivo}'"

    return False, f"se oyo '{limpio}'"


def preparar_micro(webm, carpeta):
    """Del navegador llega WebM. Whisper quiere WAV 16k; la clase, Opus nivelado.

    El loudnorm importa: 25 microfonos domesticos llegan con niveles dispares y
    uno que suene a susurro junto a otro que revienta es insoportable de oir.
    """
    carpeta = Path(carpeta)
    wav = carpeta / (Path(webm).stem + ".wav")
    opus = carpeta / (Path(webm).stem + ".opus")

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(webm),
         "-ac", "1", "-ar", "16000", str(wav)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(webm),
         "-af", "loudnorm=I=-18:TP=-2:LRA=11",
         "-c:a", "libopus", "-b:a", "32k", str(opus)],
        check=True,
    )
    return wav, opus.name
