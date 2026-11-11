import time

from app.speech.recorder import Recorder

recorder = Recorder()

print("[Misty] Session started.")

try:
    recorder.start()

    while True:
        time.sleep(0.1)

except KeyboardInterrupt:
    pass

except Exception as error:
    print(f"[Misty] Session error: {error}")

finally:
    audio = recorder.stop()

    if audio is not None:
        print(f"[Misty] Captured {len(audio)} audio samples.")

    print("[Misty] Session ended.")
