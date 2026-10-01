on run argv
    set targetTTY to item 1 of argv
    delay 2
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is targetTTY then
                    if busy of t then return
                    if (count of tabs of w) is 1 then
                        close w
                    else
                        close t
                    end if
                    return
                end if
            end repeat
        end repeat
    end tell
end run
