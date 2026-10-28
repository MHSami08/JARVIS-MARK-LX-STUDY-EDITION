"""
tools/audio_probe.py — measure the real audio round trip on THIS machine.

"Sometimes the audio lags" is a measurable property of the host API JARVIS ends
up on. This prints, per Windows host API:

    * whether the endpoint opens at the app's rates (16 kHz in / 24 kHz out)
    * the reported round-trip latency
    * whether audio actually MOVES (a fake sink opens and accepts instantly)
    * whether WASAPI auto-convert fixes the "Invalid sample rate" refusal

It then asks core/audio_devices.py which API the app would actually pick, so the
answer printed at the bottom is the one that matters.

Run:
    .venv\\Scripts\\python.exe tools\\audio_probe.py
"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import sounddevice as sd  # noqa: E402

IN_RATE, OUT_RATE = 16000, 24000


def _autoconv():
    try:
        return sd.WasapiSettings(exclusive=False, auto_convert=True)
    except Exception:
        return None


def probe_output(idx: int, api: str) -> None:
    for label, extra in (("plain", None), ("auto_convert", _autoconv())):
        if label == "auto_convert" and "wasapi" not in api.lower():
            continue
        try:
            st = sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16",
                                    blocksize=1024, device=idx, extra_settings=extra)
            lat = float(getattr(st, "latency", 0.0) or 0.0)
            st.start()
            t0 = time.monotonic()
            st.write(bytes(OUT_RATE * 2))          # 1.0 s of silence
            el = time.monotonic() - t0
            st.stop(); st.close()
            moved = "moves audio" if el > 0.4 else "FAKE (silent sink)"
            print(f"  output [{label:12s}] latency={lat*1000:6.0f} ms  "
                  f"wrote 1.00s in {el:5.2f}s  -> {moved}")
        except Exception as e:
            print(f"  output [{label:12s}] FAILED: {type(e).__name__}: {e}")


def probe_input(idx: int, api: str) -> None:
    for label, extra in (("plain", None), ("auto_convert", _autoconv())):
        if label == "auto_convert" and "wasapi" not in api.lower():
            continue
        try:
            frames = [0]

            def _cb(indata, n, *_a):
                frames[0] += n

            st = sd.InputStream(samplerate=IN_RATE, channels=1, dtype="int16",
                                blocksize=1024, device=idx, callback=_cb,
                                extra_settings=extra)
            st.start()
            time.sleep(0.35)
            st.stop(); st.close()
            ok = frames[0] > IN_RATE * 0.35 * 0.3
            print(f"  input  [{label:12s}] captured {frames[0]:6d} frames  "
                  f"-> {'real mic' if ok else 'NO AUDIO'}")
        except Exception as e:
            print(f"  input  [{label:12s}] FAILED: {type(e).__name__}: {e}")


def main() -> int:
    print(f"sounddevice {sd.__version__}")
    print(f"portaudio   {sd.get_portaudio_version()[1]}")
    print(f"rates       in={IN_RATE} Hz, out={OUT_RATE} Hz\n")

    apis = list(sd.query_hostapis())
    defaults = sd.default.device

    for i, a in enumerate(apis):
        name = a["name"]
        print(f"[{i}] {name}")
        out_idx = a["default_output_device"]
        in_idx = a["default_input_device"]
        if out_idx >= 0:
            probe_output(out_idx, name)
        if in_idx >= 0:
            probe_input(in_idx, name)
        print()

    print(f"OS default device indices: [in={defaults[0]}, out={defaults[1]}]\n")

    # ── What the app itself will choose ──────────────────────────────────────
    print("=" * 68)
    print("core/audio_devices.py selection (what JARVIS will actually use)")
    print("=" * 68)
    from core import audio_devices
    audio_devices.configure(IN_RATE, OUT_RATE)
    for kind in ("input", "output"):
        names = audio_devices.list_devices(kind, refresh=True)
        print(f"  {kind:6s}: api={audio_devices._chosen_api.get(kind)!r}  "
              f"{len(names)} device(s)")
        for n in names:
            idx = audio_devices.resolve(n, kind)
            print(f"            '{n}' -> index {idx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
