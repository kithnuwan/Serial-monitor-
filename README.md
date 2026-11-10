# Serial Monitor

A cross-platform (Windows / Linux / macOS) desktop serial terminal written in
Python + Tkinter, using [pyserial](https://pyserial.readthedocs.io/).

## Features

**Serial port settings**
- Port list with *Refresh* (any pyserial URL also works, e.g. `loop://`,
  `socket://host:port`, `rfc2217://host:port`)
- Baud rate (preset list or type any custom value)
- Data bits (5–8), parity (None/Even/Odd/Mark/Space), stop bits (1/1.5/2)
- Flow control: None, RTS/CTS, XON/XOFF, DSR/DTR
- DTR / RTS line control and live CTS / DSR / RI / CD indicators
- Settings are locked while connected and remembered between sessions

**Monitor**
- Display as **ASCII**, **HEX**, or **HEX + ASCII** dump
- Optional timestamps, show/hide sent data (`>>` sent, `<<` received, colour-coded)
- Show control characters (`<CR>`, `<LF>`, `<00>`…)
- Auto-scroll, clear, save log to file, RX/TX byte counters

**Send**
- **ASCII** mode with escape sequences: `\r \n \t \0 \\ \e \xHH`
  (e.g. `AT+GMR`, `Hello\x1B[2J`)
- **HEX** mode, flexible input: `01 03 00 00 00 0A`, `0x01,0x03`, `01030A`, `AA:BB`
- Line ending: None, CR, LF, CR+LF
- Command history (↑ / ↓ in the input box)
- Repeat send every *N* ms
- 8 quick-command buttons (ASCII or HEX) — right-click a button to edit it

## Install & run

Requires Python 3.8+ with Tkinter (included with the python.org installers on
Windows/macOS; on Debian/Ubuntu: `sudo apt install python3-tk`).

```bash
pip install -r requirements.txt
python serial_monitor.py        # or: python -m serial_monitor
```

On Linux, your user needs access to the serial device
(`sudo usermod -aG dialout $USER`, then log out and in again).

### Build a standalone executable (optional)

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name SerialMonitor serial_monitor.py
```

The executable is written to `dist/`.

## Tests

```bash
python -m unittest discover -s tests
```

Tip: connect to the port `loop://` to try the app without hardware — everything
you send is echoed back.

Settings, history and quick commands are stored in `~/.serial_monitor.json`.
