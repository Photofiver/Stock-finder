import json, subprocess, wave, pathlib
import numpy as np
from piper import PiperVoice
lines = json.loads(pathlib.Path("voice_render_lines.json").read_text())
voice = PiperVoice.load("en_US-lessac-medium.onnx")
sr=24000
out=np.zeros(round(237.815*sr),dtype=np.int16)
for i,(t,s) in enumerate(lines):
    with wave.open("temp.wav","wb") as w: voice.synthesize_wav(s,w)
    dur=float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0","temp.wav"]))
    end=lines[i+1][0] if i+1<len(lines) else 237.815
    maxdur=end-t-0.12
    tempo=max(1,dur/maxdur)
    subprocess.run(["ffmpeg","-y","-loglevel","error","-i","temp.wav","-af",f"atempo={tempo}","-ar",str(sr),"-ac","1","-f","s16le","temp.pcm"],check=True)
    seg=np.fromfile("temp.pcm",dtype=np.int16)[:int(maxdur*sr)]
    start=round(t*sr)
    out[start:start+len(seg)]=seg
with wave.open("voice.wav","wb") as w:
    w.setnchannels(1);w.setsampwidth(2);w.setframerate(sr);w.writeframes(out.tobytes())
subprocess.run(["ffmpeg","-y","-loglevel","error","-i","voice.wav","-c:a","libmp3lame","-b:a","96k","voice.mp3"],check=True)
