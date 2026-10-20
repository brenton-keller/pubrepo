# pubrepo demo: the scrub gate catches a leak, you fix it, clean publish.
# Regenerate: from a demo repo whose README.md contains the forbidden string
# acme-internal and that has run `pubrepo init`, run `vhs demo.tape`.
Output demo.gif
Set FontSize 14
Set Width 860
Set Height 360
Set Padding 10
Set Framerate 10
Set TypingSpeed 30ms
Type "pubrepo --dry-run --quiet"
Enter
Sleep 2.5s
Type "sed -i 's/acme-internal/plain old Linux/' README.md && git commit -qam fix"
Enter
Sleep 600ms
Type "pubrepo publish"
Enter
Sleep 2s
Type "y"
Enter
Sleep 3s
