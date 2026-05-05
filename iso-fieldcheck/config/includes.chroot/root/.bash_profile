# v1.1.2+ — Field-Check Live ISO first-login script.
#
# Runs on tty1 right after the autologin override drops the operator
# into a root shell. Renders the field-check report (drives + server
# identity + RAID-controller warning when applicable), then leaves
# them at a normal bash prompt for any followup (rerun the report,
# raw smartctl, journalctl, etc.).
#
# Skipped on non-tty1 (e.g. SSH from a laptop) so the report doesn't
# clobber every shell session — operator who SSHes in just gets the
# normal shell. They can rerun `driveforge-field-report` manually.
#
# Also wait briefly for the daemon to be up before rendering. The
# report renders fine without a running daemon (it queries lsblk +
# smartctl + dmidecode directly, no API call needed), but waiting
# 2-3 seconds avoids a race where lsblk runs before udev has
# settled the drives.

if [ "$(tty)" = "/dev/tty1" ]; then
    # Source standard bashrc first so PS1 / aliases are set up
    # before we drop back to the prompt.
    [ -r /root/.bashrc ] && . /root/.bashrc
    # Brief settle for udev / smartctl
    sleep 2
    # Render the report. Errors are caught inside main() — no
    # traceback escapes; we always end up at the prompt.
    driveforge-field-report
    echo
    echo "  (You are now at a root shell. Type \`driveforge-field-report\` to refresh,"
    echo "   or \`exit\` to log out and have the report re-run on the next login.)"
    echo
fi
