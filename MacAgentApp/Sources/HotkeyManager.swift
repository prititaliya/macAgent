import AppKit
import Carbon

private var gHotKeyManager: HotkeyManager?

private func macAgentHotKeyCallback(
    nextHandler: EventHandlerCallRef?,
    theEvent: EventRef?,
    userData: UnsafeMutableRawPointer?
) -> OSStatus {
    var hkID = EventHotKeyID()
    GetEventParameter(
        theEvent,
        EventParamName(kEventParamDirectObject),
        EventParamType(typeEventHotKeyID),
        nil,
        MemoryLayout<EventHotKeyID>.size,
        nil,
        &hkID
    )
    if hkID.signature == OSType(0x4D414754) { // 'MAGT'
        DispatchQueue.main.async {
            HotkeyManager.shared.onToggle?()
        }
    }
    return noErr
}

/// Global ⌃⌥Space — Carbon hotkey + Accessibility prompt.
final class HotkeyManager {
    static let shared = HotkeyManager()
    var onToggle: (() -> Void)?

    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private var localMonitor: Any?
    private var globalMonitor: Any?

    func registerDefault() {
        unregister()
        gHotKeyManager = self
        promptAccessibilityIfNeeded()

        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        InstallEventHandler(
            GetEventDispatcherTarget(),
            macAgentHotKeyCallback,
            1,
            &eventType,
            nil,
            &handlerRef
        )

        let keyID = EventHotKeyID(signature: OSType(0x4D414754), id: 1)
        let status = RegisterEventHotKey(
            UInt32(kVK_Space),
            UInt32(controlKey | optionKey),
            keyID,
            GetEventDispatcherTarget(),
            0,
            &hotKeyRef
        )
        if status != noErr {
            NSLog("MacAgent: Carbon hotkey failed (%d); using NSEvent monitors", status)
        }

        // Local (when our windows are key)
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if Self.matchesToggle(event) {
                self?.onToggle?()
                return nil
            }
            return event
        }
        // Global (other apps) — needs Accessibility
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if Self.matchesToggle(event) {
                DispatchQueue.main.async { self?.onToggle?() }
            }
        }
    }

    static func matchesToggle(_ event: NSEvent) -> Bool {
        guard event.keyCode == 49 else { return false } // space
        let mods = event.modifierFlags.intersection([.control, .option, .command, .shift])
        return mods.contains(.control) && mods.contains(.option) && !mods.contains(.command)
    }

    func unregister() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let handlerRef {
            RemoveEventHandler(handlerRef)
            self.handlerRef = nil
        }
        if let localMonitor {
            NSEvent.removeMonitor(localMonitor)
            self.localMonitor = nil
        }
        if let globalMonitor {
            NSEvent.removeMonitor(globalMonitor)
            self.globalMonitor = nil
        }
    }

    private func promptAccessibilityIfNeeded() {
        let trustedSilent = AXIsProcessTrusted()
        if trustedSilent {
            return
        }
        let defaults = UserDefaults.standard
        let promptedKey = "didPromptAccessibility"
        // Only show the system dialog once (first launch / first need).
        if defaults.bool(forKey: promptedKey) {
            NSLog("MacAgent: Accessibility still off — enable in System Settings → Privacy & Security → Accessibility.")
            return
        }
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
        defaults.set(true, forKey: promptedKey)
        NSLog("MacAgent: Prompted for Accessibility (once). Grant it for ⌃⌥Space + UI control.")
    }
}