# Runs before main.py on every startup. If an OTA-updated firmware fails
# to boot 3 times in a row, the previous version is restored.
from holdfast.ota import boot_check

boot_check()
