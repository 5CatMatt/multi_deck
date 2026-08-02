; AutoHotkey v2 helpers callable from deck.json via {"type":"ahk","fn":"..."}.
;
; AutoHotkey handles window manipulation far better than Python does, so the split is:
; Python orchestrates and decides what to do, AHK does the actual window and input work.

#Requires AutoHotkey v2.0

SnapLeft() {
    Send("#{Left}")
}

SnapRight() {
    Send("#{Right}")
}

Maximise() {
    Send("#{Up}")
}

Minimise() {
    Send("#{Down}")
}

; Moves the active window to the next monitor.
NextMonitor() {
    Send("#+{Right}")
}

; Focuses an existing window matching a title, or does nothing if there is none.
; Useful when a "launch" action would otherwise open a second copy.
FocusWindow(title) {
    if WinExist(title) {
        WinActivate(title)
    }
}

; Types text without the clipboard, so it survives apps that block paste.
TypeText(text) {
    SendText(text)
}

; Sets the system volume to an absolute percentage.
SetVolume(percent) {
    SoundSetVolume(Integer(percent))
}
