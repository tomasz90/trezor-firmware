from typing import TYPE_CHECKING

import storage.device as storage_device
from storage.cache_common import APP_COMMON_BUSY_DEADLINE_MS
from trezor import config, io, utils, wire, workflow
from trezor.wire import context
from trezor.wire.message_handler import filters, remove_filter

if utils.USE_POWER_MANAGER:
    from trezor.power_management.autodim import autodim_display
    from trezor.power_management.suspend import suspend_device
    from trezorui_api import BacklightLevels, backlight_fade

if utils.USE_BLE:
    import trezorble as ble

if TYPE_CHECKING:
    from trezor import protobuf
    from trezor.wire import Handler, Msg

_SCREENSAVER_IS_ON = False


def _session_is_valid() -> bool:
    """Return True if a session is configured and the device is unlocked.

    The idle timer (set to session_timeout_ms) handles expiry by locking the
    device after inactivity. This function only checks whether the session
    feature is enabled and the device is currently unlocked.
    """
    if not storage_device.get_session_timeout_ms():
        return False
    return config.is_unlocked()


if not utils.USE_POWER_MANAGER:

    def notify_suspend() -> None:
        pass

    def signal_long_press_lock() -> None:
        pass

else:
    from trezor import loop

    _SHOULD_SUSPEND = False
    _notify_power_button: loop.mailbox[None] = loop.mailbox()
    _notify_long_press_lock: loop.mailbox[None] = loop.mailbox()
    notify_bootscreen: loop.mailbox[None] = loop.mailbox()

    # RGBLED_RED = RGB_COMPOSE_COLOR(100, 6, 3)
    _RGBLED_RED: int = (100 << 16) | (6 << 8) | 3

    def signal_long_press_lock() -> None:
        """Called from UI event handler to schedule async lock (avoids re-entrancy crash)."""
        _notify_long_press_lock.put(None, replace=True)

    async def _long_press_handler() -> None:
        """Handles long power button press: blink red LED (if session ON) and lock, all async-safe."""
        while True:
            await _notify_long_press_lock
            if utils.USE_RGB_LED and _session_is_valid():
                io.rgb_led.rgb_led_set_color(_RGBLED_RED)
                await loop.sleep(150)
                io.rgb_led.rgb_led_set_color(0)
                await loop.sleep(100)
                io.rgb_led.rgb_led_set_color(_RGBLED_RED)
                await loop.sleep(150)
                io.rgb_led.rgb_led_set_color(0)
            lock_device_if_unlocked()

    def _schedule_suspend_after_workflow() -> None:
        """Signal that the device should be suspended by the default task after the
        running workflows finish.

        Sets a suspend homescreen for next time the default task is invoked.
        """
        global _SHOULD_SUSPEND

        backlight_fade(BacklightLevels.NONE)
        _SHOULD_SUSPEND = True
        set_homescreen()

    def notify_suspend() -> None:
        """Signal that the device should be suspended in the next cycle.

        Notifies an asynchronous task to perform the suspend in a separate thread.
        """
        notify_bootscreen.put(None, replace=True)
        _notify_power_button.put(None, replace=True)

    async def _power_handler() -> None:
        """Handler for the notify_suspend signal."""
        while True:
            await _notify_power_button
            if _session_is_valid():
                # Session active: sleep without locking
                if not utils.EMULATOR:
                    if workflow.autolock_interrupts_workflow:
                        suspend_and_resume()
                    else:
                        _schedule_suspend_after_workflow()
            else:
                lock_device_if_unlocked()

    def suspend_and_resume() -> None:
        """Suspend Trezor and handle wakeup.

        The function will only return after Trezor has woken up.
        """
        from trezor.ui import CURRENT_LAYOUT, display

        global _SHOULD_SUSPEND

        # fadeout the screen
        backlight_fade(BacklightLevels.NONE)
        # suspend the device
        suspend_device()

        # reconfigure drivers
        display.orientation(storage_device.get_rotation())
        if utils.USE_HAPTIC:
            io.haptic.haptic_set_enabled(storage_device.get_haptic_feedback())
        if utils.USE_RGB_LED:
            io.rgb_led.rgb_led_set_enabled(storage_device.get_rgb_led())

        # redraw the screen and touch idle timer
        workflow.idle_timer.touch()
        if CURRENT_LAYOUT is not None:
            CURRENT_LAYOUT.repaint()

        _SHOULD_SUSPEND = False
        set_homescreen()

    async def _suspend_and_resume_task() -> None:
        """Task to suspend Trezor and handle wakeup.

        Must be async so that we can schedule it via set_default.
        """
        suspend_and_resume()

    def _do_suspend() -> None:
        """Suspend the screen (no PIN required on wake)."""
        if workflow.autolock_interrupts_workflow:
            suspend_and_resume()
        else:
            _schedule_suspend_after_workflow()

    def _autolock_usb() -> None:
        """USB idle timeout: suspend screen if session is active, lock device if not."""
        if _session_is_valid():
            if not utils.EMULATOR:
                _do_suspend()
        else:
            lock_device_if_unlocked()

    def lock_device_if_unlocked_on_battery() -> None:
        """Lock the device if it is unlocked and running on battery or wireless charger."""
        if io.pm.is_usb_connected():
            return
        if _session_is_valid():
            if not utils.EMULATOR:
                _do_suspend()
        else:
            lock_device_if_unlocked()

    def configure_autodim() -> None:
        """Configure the autodim setting via idle timer."""
        workflow.idle_timer.set(storage_device.AUTODIM_DELAY_MS, autodim_display)
        workflow.idle_timer.set(
            storage_device.get_autolock_delay_battery_ms(),
            lock_device_if_unlocked_on_battery,
        )


def set_homescreen() -> None:
    import storage.recovery as storage_recovery

    from apps.common import backup

    set_default = workflow.set_default  # local_cache_attribute

    if utils.USE_POWER_MANAGER and _SHOULD_SUSPEND:
        set_default(_suspend_and_resume_task)

    elif context.cache_is_set(APP_COMMON_BUSY_DEADLINE_MS):
        from apps.homescreen import busyscreen

        set_default(busyscreen)

    elif not config.is_unlocked():
        from apps.homescreen import lockscreen

        set_default(lockscreen)

    elif _SCREENSAVER_IS_ON:
        from apps.homescreen import screensaver

        set_default(screensaver, restart=True)

    elif storage_recovery.is_in_progress() or backup.repeated_backup_enabled():
        from apps.management.recovery_device.homescreen import recovery_homescreen

        set_default(recovery_homescreen)

    else:
        from apps.homescreen import homescreen

        set_default(homescreen)


def lock_device(interrupt_workflow: bool = True) -> None:
    if config.has_pin():
        config.lock()
        filters.append(_pinlock_filter)
        set_homescreen()
        if interrupt_workflow:
            workflow.close_others()
        # TODO: should we suspend the device here?
        utils.notify_send(utils.NOTIFY_SOFTLOCK)


def lock_device_if_unlocked() -> None:
    from apps.common.request_pin import can_lock_device

    if not utils.USE_BACKLIGHT and not can_lock_device():
        # on OLED devices without PIN, trigger screensaver
        global _SCREENSAVER_IS_ON

        _SCREENSAVER_IS_ON = True
        set_homescreen()

    elif config.is_unlocked():
        lock_device(interrupt_workflow=workflow.autolock_interrupts_workflow)

    if utils.USE_POWER_MANAGER and not utils.EMULATOR:
        if workflow.autolock_interrupts_workflow:
            if not config.is_unlocked():
                # locking during PIN entering should restart the workflow, otherwise the keyboard input field is not cleared
                workflow.close_others()
            # suspend immediately
            suspend_and_resume()
        else:
            # set a suspending homescreen
            _schedule_suspend_after_workflow()


async def unlock_device() -> None:
    """Ensure the device is in unlocked state.

    If the storage is locked, attempt to unlock it. Reset the homescreen and the wire
    handler.
    """
    from apps.common.request_pin import verify_user_pin

    global _SCREENSAVER_IS_ON

    if not config.is_unlocked():
        # verify_user_pin will raise if the PIN was invalid
        await verify_user_pin()
        # non-public config settings are now available
        reload_settings_from_storage()

    _SCREENSAVER_IS_ON = False
    set_homescreen()
    remove_filter(_pinlock_filter)
    utils.notify_send(utils.NOTIFY_SOFTUNLOCK)


def _pinlock_filter(msg_type: int, prev_handler: Handler[Msg]) -> Handler[Msg]:
    if msg_type in workflow.ALLOW_WHILE_LOCKED:
        return prev_handler

    async def wrapper(msg: Msg) -> protobuf.MessageType:
        await unlock_device()
        return await prev_handler(msg)

    return wrapper


def _lock_on_session_timeout() -> None:
    """Lock the device when the session idle timer expires."""
    lock_device_if_unlocked()


# this function is also called when handling ApplySettings
def reload_settings_from_storage() -> None:
    from trezor import ui

    if utils.USE_POWER_MANAGER:
        # On T3W1: USB autolock suspends screen when session active, locks when not.
        workflow.idle_timer.set(storage_device.get_autolock_delay_ms(), _autolock_usb)
        session_ms = storage_device.get_session_timeout_ms()
        if session_ms > 0:
            workflow.idle_timer.set(session_ms, _lock_on_session_timeout)
        else:
            workflow.idle_timer.remove(_lock_on_session_timeout)
    else:
        workflow.idle_timer.set(storage_device.get_autolock_delay_ms(), lock_device_if_unlocked)

    if utils.USE_POWER_MANAGER:
        configure_autodim()

    if utils.USE_HAPTIC:
        io.haptic.haptic_set_enabled(storage_device.get_haptic_feedback())

    if utils.USE_RGB_LED:
        io.rgb_led.rgb_led_set_enabled(storage_device.get_rgb_led())

    if utils.USE_BLE:
        ble.set_enabled(storage_device.get_ble())

    wire.message_handler.EXPERIMENTAL_ENABLED = (
        storage_device.get_experimental_features()
    )
    if ui.display.orientation() != storage_device.get_rotation():
        ui.backlight_fade(ui.BacklightLevels.DIM)
        ui.display.orientation(storage_device.get_rotation())


def boot() -> None:
    set_homescreen()
    if utils.USE_POWER_MANAGER:
        loop.schedule(_power_handler())
        loop.schedule(_long_press_handler())
