# pubrepo demo: the scrub gate catches a leak, you fix it, clean publish.
# Regenerate: vhs demo.tape (needs a demo repo with an acme-internal leak)
Output demo.gif
Set FontSize 15
Set Width 920
Set Height 460
Set Padding 12
Set TypingSpeed 35ms
Type "pubrepo"
Enter
Sleep 3s
Type "sed -i 's/acme-internal/plain old Linux/' README.md"
Enter
Sleep 800ms
Type "pubrepo"
Enter
Sleep 3.5s
