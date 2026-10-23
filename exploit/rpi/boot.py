try:
    import storage
    storage.disable_usb_drive()
except Exception:
    pass
try:
    import usb_cdc
    usb_cdc.disable()
except Exception:
    pass
try:
    import usb_midi
    usb_midi.disable()
except Exception:
    pass
import usb_hid
usb_hid.enable((usb_hid.Device.KEYBOARD,), boot_device=1)
