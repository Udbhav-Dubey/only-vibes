# lyrics_visualize

A terminal audio visualizer with synced lyrics.

![screenshot](./screenshot.png)

It listens to whatever my laptop is playing, draws smooth colored bars in the terminal that move with the music, and shows the current lyric line above them, in time with the song. All in one Python file, `spectrum.py`.


## What it does

- **Bars:** captures the system's output audio, runs an FFT, and draws log-spaced bars with peak caps in truecolor, using 1/8-height block characters so the motion looks smooth.
- **Lyrics:** reads the current song and position from whatever player is running, fetches time-synced lyrics from [LRCLIB](https://lrclib.net) (free, no API key), and highlights the current line.
- **Phone screen (optional):** with `--phone`, the same lyrics are also served to a browser on any device on the network, so an old phone can sit on the desk as a lyrics display.

## Requirements

Built and tested on Manjaro (PipeWire). Should work on any Linux with PulseAudio or PipeWire-pulse.

```bash
sudo pacman -S libpulse python-numpy playerctl
```

- `libpulse` gives `parec`, which captures the audio
- `python-numpy` does the FFT
- `playerctl` reads what's playing (artist, title, position)

Your terminal needs truecolor support. Most modern ones have it.

## Run it

```bash
python spectrum.py
```

Some options:

```bash
python spectrum.py --theme sunset --gain -6
python spectrum.py --player firefox --lyric-offset 0.3
python spectrum.py --no-lyrics
python spectrum.py --phone
```

| Option | What it does |
|--------|--------------|
| `--theme` | `neon` (default), `fire`, `matrix`, `ice`, `sunset` |
| `--gain` | Extra gain in dB. Go negative if the bars are too tall |
| `--range` | How many dB the bars span. Smaller means more contrast |
| `--decay` | How fast bars fall. Higher is smoother, lower is snappier |
| `--width`, `--gap` | Bar width and spacing in columns |
| `--player` | Pick a specific player for lyrics, e.g. `firefox`, `spotify` |
| `--lyric-offset` | Shift lyrics by this many seconds |
| `--source` | Capture from a specific audio source instead of the default |
| `--no-lyrics` | Bars only |
| `--phone` | Also serve the lyrics to a phone/tablet browser |
| `--port` | Port for `--phone` (default 8765) |

Keys while running:

| Key | What it does |
|-----|--------------|
| `,` `.` | Jump to the previous / next lyric line |
| `[` `]` | Nudge lyrics 0.5 s later / earlier |
| `0` | Reset the nudge |
| `Ctrl-C` | Quit |

## Using an old phone as a lyrics screen

```bash
python spectrum.py --phone
```
![screenshot](./old_phone.jpeg) 

The terminal shows a `phone: http://<laptop-ip>:8765` address under the lyrics. Open it in the phone's browser (same WiFi as the laptop), then tap the screen once to go fullscreen.

The page shows four lyric lines on a black background with the current one big and glowing, and the lines slide up as the song moves along. It updates over Server-Sent Events, so nothing needs installing on the phone and Termux isn't needed.

Tips:
- If it won't load, the laptop's firewall may be blocking the port.
- To keep the phone's screen awake, turn on "stay awake while charging" in its developer options.
- Text size: add `?size=1.3` to the address to make everything 30% bigger (or `?size=0.8` for smaller).
- Keys (`,` `.` `[` `]`) fix the sync for the terminal and the phone at once.

## How it works (short version)

1. `parec` streams the audio from the output's monitor source. A thread keeps the latest 4096 samples.
2. Every frame: Hann window, FFT, group bins into log-spaced bands, convert to dB, tilt the spectrum so the bass doesn't dominate, smooth it, draw it.
3. A second thread polls `playerctl` for the song and position. A third fetches lyrics from LRCLIB when the song changes.
4. The current lyric line is found with a binary search on the timestamps, using the playback position.
5. With `--phone`, a tiny built-in web server (Python's `http.server`) serves one HTML page and pushes the lines around the current one to it whenever they change.

## Known quirks

- Works best with YouTube Music, which reports a proper playback position. Regular YouTube in Firefox often reports a stuck position, so the script keeps its own clock and shows "position estimated". Use `,` `.` to catch up if the lyrics are off.
- Lyrics come from LRCLIB, so obscure songs, remixes and odd uploads may show "no lyrics found".
- Lyrics in non-Latin scripts may not center perfectly.

## Status

Works on my machine. Ideas for later: a karaoke-style highlight on the current line, other modes (waterfall, braille wave), and maybe a small ESP32 display on the desk.
