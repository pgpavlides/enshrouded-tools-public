# Enshrouded Tools

A Windows tray app for Enshrouded: key spam, turbo E, a fishing bot, spin, and a route compass with a minimap.

## Install or update

Open PowerShell and run:

```powershell
irm https://raw.githubusercontent.com/pgpavlides/enshrouded-tools-public/main/install.ps1 | iex
```

The app goes to `%LOCALAPPDATA%\EnshroudedTools`, and a shortcut is added to the desktop. If Python 3.10 or newer is missing, the installer gets Python 3.13 with winget. Run the same line again to update; your settings and routes are kept.

## Hotkeys

| Key | Action |
|---|---|
| Hold E | Spam E |
| Shift+E | Turbo E on/off |
| F8 | Fishing bot on/off |
| F9 | Spin on/off |
| F6 / Shift+F6 | Save a route spot / delete the nearest one |
| F7 / Shift+F7 | Route compass on/off / skip a spot |
| F5 / Shift+F5 | Minimap on/off / change zoom |
| Ctrl+F12 | Quit |

Double-click the tray icon for settings.

## Notes

- The route features read the player position from the game's memory (read only). A game patch can break them.
- The fishing bot saves fight screenshots to `shots/`. Set "Record fight frames every" to 0 to turn this off.
- The world map image is not included, so the Map window shows no background.
- This is an unofficial tool, not made by or affiliated with the game's developers. Use it at your own risk.
