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

; Waits for a window to exist, focuses it, and pastes the clipboard into it.
;
; This exists to remove the race in "launch an app, then Ctrl+V". A fixed `delay` step cannot
; win: too short and the paste fires while the previous window still holds focus — so a
; screenshot lands in whatever you were typing in — and too long makes every use of the tile
; feel broken. Waiting on the window itself is both faster and correct.
;
; `title` is any AHK v2 window spec; "ahk_exe mspaint.exe" is the useful form here. Gives up
; quietly after `timeout` seconds rather than pasting somewhere unintended.
;
; `after` is an optional extra Send, in AHK key syntax, fired once the paste has landed —
; "^+x" crops Paint to the pasted selection. It belongs in here rather than as a following
; `hid` step in the sequence, so that it shares the timeout guard above: a trailing step would
; fire blind into whatever happened to be focused on the run where the window never appeared,
; which is exactly the case this function exists to make safe.
PasteInto(title, after := "", timeout := 8) {
    if !WinWait(title, , Integer(timeout)) {
        return
    }
    WinActivate(title)
    WinWaitActive(title, , 2)
    Sleep(120)  ; a freshly-mapped window can accept focus a beat before it accepts input
    Send("^v")

    if (after != "") {
        Sleep(120)  ; let the paste settle; a crop needs the selection to exist first
        Send(after)
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
