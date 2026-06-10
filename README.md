# Blender Multitouch Viewport

A Windows-only Blender 5.x addon that enables native touchscreen input — orbit, pan, zoom, scroll, and panel interaction using a touchscreen or touch-enabled display.

## Features

- **3D Viewport**
  - 1-finger drag → orbit
  - 2-finger drag → pan
  - 2-finger pinch → zoom

- **Panels, menus, and everywhere else**
  - Touch and drag → scroll
  - Choose between 1-finger or 2-finger scrolling in the plugin panel

- **Stop button** in the sidebar panel (or press ESC in the 3D viewport)

- Properly suppresses Blender's default touch-as-mouse behaviour so scrolling does not also trigger accidental selections or drags

## Requirements

- Windows 10 or 11
- Blender 5.x (tested on 5.1.2)
- A touchscreen or touch-enabled display

## Installation

1. Download `multitouch_viewport.py`
2. In Blender: **Edit → Preferences → Add-ons → Install**
3. Browse to the downloaded file and install it
4. Enable the addon (search for "Multitouch")

## Usage

Open the **N-panel** in the 3D Viewport (press `N`), go to the **Touch** tab, and click **Start Multitouch**. Press **ESC** or click **Stop Multitouch** to deactivate.

### Scroll fingers setting

In the Touch panel you can toggle between **1 Finger** and **2 Fingers** scroll mode for panels and menus. 2-finger mode is useful if you want single-finger touches to pass through to Blender normally.

## How it works

The addon installs a thread-local `WH_GETMESSAGE` hook on Blender's GUI thread that:

1. Captures `WM_POINTER` touch events and feeds them into the gesture state machine
2. Suppresses the synthesised `WM_LBUTTON*` / `WM_RBUTTON*` / `WM_MBUTTON*` mouse messages Windows auto-generates from touch contacts while any finger is on screen, preventing Blender from double-acting on the same input

The hook is thread-local — other applications are completely unaffected.

## License

MIT

