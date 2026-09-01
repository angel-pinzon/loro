"""Voz del loro: sintetiza con OmniVoice y empaqueta en Opus para difundir.

Se usa 'voice design' (instruct) y no clonacion: el loro no es la voz de nadie
real, evita el acento español sobre el ingles y ocupa la mitad de VRAM porque
sin audio de referencia no hace falta cargar Whisper para transcribirlo.
"""

import hashlib
import subprocess
import threading
from pathlib import Path

import soundfile as sf
import torch

MEDIA = Path(__file__).parent / "media"


class Voz:
    def __init__(self, config):
        self.ajustes = config["voz"]
        self.modelo = None
        # La GPU es una sola: las peticiones se serializan aunque lleguen a la vez.
        self.turno = threading.Lock()

    def cargar(self):
        from omnivoice import OmniVoice

        self.modelo = OmniVoice.from_pretrained(
            "k2-fsa/OmniVoice", device_map="cuda:0", dtype=torch.float16
        )
        MEDIA.mkdir(exist_ok=True)

    def decir(self, texto, idioma="English"):
        """Devuelve el nombre del .opus con el texto ya hablado.

        Frases que se repiten mucho ("Correct!", "Let's go!") salen de cache y
        cuestan cero: el nombre del archivo es el hash del texto y los ajustes.
        """
        firma = f"{texto}|{idioma}|{self.ajustes['instruct']}|{self.ajustes['num_step']}"
        nombre = hashlib.sha1(firma.encode("utf-8")).hexdigest()[:16] + ".opus"
        destino = MEDIA / nombre
        if destino.exists():
            return nombre

        with self.turno:
            audio = self.modelo.generate(
                text=texto,
                language=idioma,
                instruct=self.ajustes["instruct"],
                num_step=self.ajustes["num_step"],
            )[0]

        crudo = destino.with_suffix(".wav")
        sf.write(crudo, audio, self.modelo.sampling_rate)
        a_opus(crudo, destino, self.ajustes["bitrate_opus"])
        crudo.unlink(missing_ok=True)
        return nombre


def a_opus(origen, destino, bitrate="32k"):
    """WAV crudo son 384 kbps por oyente; en Opus son 31. Con 25 niños, decide."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(origen),
         "-af", "loudnorm=I=-18:TP=-2:LRA=11",
         "-c:a", "libopus", "-b:a", bitrate, str(destino)],
        check=True,
    )
    return destino
