import Foundation
import AppKit
import ApplicationServices
import Network

/// Localhost bridge: Python daemon → MacAgent.app (holds Accessibility TCC).
final class UIBridgeServer {
    static let shared = UIBridgeServer()
    static let port: UInt16 = 8082

    private var listener: NWListener?
    private let queue = DispatchQueue(label: "com.macagent.uibridge")

    func start() {
        queue.async { [weak self] in
            self?._startLocked()
        }
    }

    func stop() {
        queue.async { [weak self] in
            self?.listener?.cancel()
            self?.listener = nil
        }
    }

    private func _startLocked() {
        guard listener == nil else { return }
        do {
            let parameters = NWParameters.tcp
            let port = NWEndpoint.Port(integerLiteral: Self.port)
            let nwListener = try NWListener(using: parameters, on: port)
            nwListener.newConnectionHandler = { [weak self] connection in
                self?.handleConnection(connection)
            }
            nwListener.start(queue: queue)
            listener = nwListener
            NSLog("MacAgent: UI bridge on 127.0.0.1:%u", Self.port)
        } catch {
            NSLog("MacAgent: UI bridge failed: %@", String(describing: error))
        }
    }

    private func handleConnection(_ connection: NWConnection) {
        connection.start(queue: queue)
        readRequest(connection: connection, buffer: Data())
    }

    private func readRequest(connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { [weak self] data, _, isComplete, error in
            guard let self = self else { return }
            var buf = buffer
            if let data = data {
                buf.append(data)
            }
            if let body = Self.extractHTTPBody(buf) {
                let response = self.dispatch(body)
                self.sendHTTP(connection: connection, json: response)
                return
            }
            if isComplete || error != nil {
                // Incomplete headers/body — never pretend it was a valid JSON request.
                if !buf.isEmpty {
                    self.sendHTTP(
                        connection: connection,
                        json: ["ok": false, "error": "incomplete HTTP request"]
                    )
                } else {
                    connection.cancel()
                }
                return
            }
            self.readRequest(connection: connection, buffer: buf)
        }
    }

    private func sendHTTP(connection: NWConnection, json: [String: Any]) {
        let payload = (try? JSONSerialization.data(withJSONObject: json, options: []))
            ?? Data("{\"ok\":false}".utf8)
        var header = "HTTP/1.1 200 OK\r\n"
        header += "Content-Type: application/json\r\n"
        header += "Content-Length: \(payload.count)\r\n"
        header += "Connection: close\r\n\r\n"
        var packet = Data(header.utf8)
        packet.append(payload)
        connection.send(content: packet, completion: .contentProcessed { _ in
            connection.cancel()
        })
    }

    /// Return the body only when headers are complete AND Content-Length bytes have arrived.
    private static func extractHTTPBody(_ data: Data) -> Data? {
        guard data.count >= 4 else { return nil }
        let bytes = [UInt8](data)
        var headerEnd: Int?
        for i in 0...(bytes.count - 4) {
            if bytes[i] == 13, bytes[i + 1] == 10, bytes[i + 2] == 13, bytes[i + 3] == 10 {
                headerEnd = i + 4
                break
            }
        }
        guard let start = headerEnd else { return nil }
        let headerData = Data(bytes[0..<start])
        let headerText = String(data: headerData, encoding: .utf8) ?? ""
        var contentLength: Int?
        for line in headerText.split(whereSeparator: \.isNewline) {
            let lower = line.lowercased()
            if lower.hasPrefix("content-length:") {
                let raw = line.dropFirst("content-length:".count)
                    .trimmingCharacters(in: .whitespaces)
                contentLength = Int(raw)
                break
            }
        }
        let available = bytes.count - start
        if let need = contentLength {
            if available < need { return nil }
            return Data(bytes[start..<(start + need)])
        }
        // No Content-Length: only accept if the connection already finished (caller
        // passes complete buffers). Prefer waiting — return nil until isComplete path.
        if available == 0 { return nil }
        return Data(bytes[start...])
    }

    private func dispatch(_ data: Data) -> [String: Any] {
        guard !data.isEmpty else {
            return ["ok": false, "error": "empty request body"]
        }
        guard
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let op = obj["op"] as? String
        else {
            let preview = String(data: data.prefix(200), encoding: .utf8) ?? ""
            return [
                "ok": false,
                "error": "invalid request",
                "preview": preview,
            ]
        }

        return DispatchQueue.main.sync {
            switch op {
            case "ping":
                return ["ok": true, "trusted": AXIsProcessTrusted()]
            case "ensure_accessibility":
                return Self.ensureAccessibility(prompt: true)
            case "snapshot":
                return snapshot(limit: (obj["limit"] as? Int) ?? 40)
            case "click":
                return click(
                    name: (obj["name"] as? String) ?? "",
                    role: (obj["role"] as? String) ?? "button",
                    index: (obj["index"] as? Int) ?? 1
                )
            case "type":
                return typeText(
                    (obj["text"] as? String) ?? "",
                    app: (obj["app"] as? String) ?? ""
                )
            case "key":
                return keyStroke(
                    key: (obj["key"] as? String) ?? "return",
                    modifiers: (obj["modifiers"] as? String) ?? ""
                )
            case "menu":
                return menu(
                    app: (obj["app"] as? String) ?? "",
                    path: (obj["menu_path"] as? String) ?? ""
                )
            default:
                return ["ok": false, "error": "unknown op"]
            }
        }
    }

    /// Prompt (or re-check) Accessibility. Ad-hoc rebuilds often need toggle off→on again.
    static func ensureAccessibility(prompt: Bool) -> [String: Any] {
        if AXIsProcessTrusted() {
            return [
                "ok": true,
                "trusted": true,
                "message": "Accessibility is granted for this MacAgent.app.",
            ]
        }
        if prompt {
            let opts = [
                kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true,
            ] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(opts)
        }
        let trusted = AXIsProcessTrusted()
        return [
            "ok": trusted,
            "trusted": trusted,
            "error": trusted
                ? ""
                : (
                    "macOS still reports MacAgent as untrusted for Accessibility. "
                    + "In System Settings → Privacy & Security → Accessibility: "
                    + "turn MacAgent OFF, then ON again, then Quit and reopen MacAgent.app. "
                    + "After each rebuild/DMG install the toggle often needs that reset "
                    + "(ad-hoc signature changes)."
                ),
        ]
    }

    private func runAppleScript(_ source: String) -> (Bool, String, String) {
        var error: NSDictionary?
        guard let script = NSAppleScript(source: source) else {
            return (false, "", "script create failed")
        }
        let result = script.executeAndReturnError(&error)
        if let error = error {
            let msg = (error[NSAppleScript.errorMessage] as? String) ?? "\(error)"
            return (false, "", msg)
        }
        return (true, result.stringValue ?? "", "")
    }

    private func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
    }

    private func snapshot(limit: Int) -> [String: Any] {
        let ax = Self.ensureAccessibility(prompt: true)
        guard ax["trusted"] as? Bool == true else {
            return [
                "ok": false,
                "error": (ax["error"] as? String)
                    ?? "MacAgent needs Accessibility enabled for MacAgent.app.",
            ]
        }
        let front = runAppleScript(
            "tell application \"System Events\" to get name of first process whose frontmost is true"
        )
        guard front.0 else { return ["ok": false, "error": front.2] }
        let app = front.1
        let script = """
        tell application "System Events"
          tell process "\(esc(app))"
            set out to {}
            try
              set win to front window
              set out to out & {("window:" & (name of win as text))}
            end try
            try
              repeat with b in (buttons of front window)
                set end of out to ("button:" & (name of b as text))
                if (count of out) > \(limit) then exit repeat
              end repeat
            end try
            try
              repeat with t in (static texts of front window)
                set end of out to ("text:" & (name of t as text))
                if (count of out) > \(limit) then exit repeat
              end repeat
            end try
            set AppleScript's text item delimiters to linefeed
            return out as text
          end tell
        end tell
        """
        let r = runAppleScript(script)
        return [
            "ok": true,
            "app": app,
            "elements": String(r.1.prefix(3500)),
            "warning": r.0 ? "" : r.2,
        ]
    }

    private func click(name: String, role: String, index: Int) -> [String: Any] {
        let ax = Self.ensureAccessibility(prompt: true)
        guard ax["trusted"] as? Bool == true else {
            return [
                "ok": false,
                "error": (ax["error"] as? String)
                    ?? "MacAgent needs Accessibility enabled.",
            ]
        }
        let front = runAppleScript(
            "tell application \"System Events\" to get name of first process whose frontmost is true"
        )
        guard front.0 else { return ["ok": false, "error": front.2] }
        let app = front.1
        let roleMap = [
            "button": "button", "checkbox": "checkbox", "radio": "radio button",
            "menu": "menu item", "menuitem": "menu item",
        ]
        let asRole = roleMap[role.lowercased()] ?? "button"
        let script: String
        if name.isEmpty {
            let idx = max(1, index)
            script = """
            tell application "System Events"
              tell process "\(esc(app))"
                set frontmost to true
                try
                  click \(asRole) \(idx) of front window
                  return "clicked_index:\(idx)"
                on error errMsg
                  return "error:" & errMsg
                end try
              end tell
            end tell
            """
        } else {
            script = """
            tell application "System Events"
              tell process "\(esc(app))"
                set frontmost to true
                try
                  click \(asRole) "\(esc(name))" of front window
                  return "clicked:\(esc(name))"
                on error errMsg
                  try
                    click UI element "\(esc(name))" of front window
                    return "clicked_element:\(esc(name))"
                  on error errMsg2
                    return "error:" & errMsg2
                  end try
                end try
              end tell
            end tell
            """
        }
        let r = runAppleScript(script)
        if !r.0 || r.1.hasPrefix("error:") {
            return ["ok": false, "app": app, "error": r.1.isEmpty ? r.2 : r.1]
        }
        return ["ok": true, "app": app, "result": r.1, "name": name, "role": role]
    }

    private func typeText(_ text: String, app: String = "") -> [String: Any] {
        let ax = Self.ensureAccessibility(prompt: true)
        guard ax["trusted"] as? Bool == true else {
            return [
                "ok": false,
                "error": (ax["error"] as? String)
                    ?? "MacAgent needs Accessibility enabled.",
            ]
        }
        if !app.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            _ = runAppleScript("tell application \"\(esc(app))\" to activate")
        } else {
            // Overlay often steals focus after open_app — bump current frontmost.
            _ = runAppleScript("""
            tell application "System Events"
              set frontApp to first process whose frontmost is true
              set frontmost of frontApp to true
            end tell
            """)
        }
        Thread.sleep(forTimeInterval: 0.2)
        let r = runAppleScript("""
        tell application "System Events"
          keystroke "\(esc(text))"
        end tell
        """)
        if !r.0 { return ["ok": false, "error": r.2] }
        return [
            "ok": true,
            "typed_chars": text.count,
            "app": app,
            "result": "typed",
        ]
    }

    private func keyStroke(key: String, modifiers: String) -> [String: Any] {
        let ax = Self.ensureAccessibility(prompt: true)
        guard ax["trusted"] as? Bool == true else {
            return [
                "ok": false,
                "error": (ax["error"] as? String)
                    ?? "MacAgent needs Accessibility enabled.",
            ]
        }
        let keyCodes: [String: Int] = [
            "return": 36, "enter": 76, "escape": 53, "esc": 53,
            "tab": 48, "space": 49, "delete": 51, "backspace": 51,
            "left": 123, "right": 124, "down": 125, "up": 126,
        ]
        var mods: [String] = []
        for part in modifiers.lowercased().split(whereSeparator: { ", ".contains($0) }) {
            switch String(part) {
            case "cmd", "command", "meta": mods.append("command down")
            case "shift": mods.append("shift down")
            case "option", "alt": mods.append("option down")
            case "control", "ctrl": mods.append("control down")
            default: break
            }
        }
        let using = mods.isEmpty ? "" : " using {\(mods.joined(separator: ", "))}"
        let k = key.lowercased()
        let script: String
        if let code = keyCodes[k] {
            script = "tell application \"System Events\" to key code \(code)\(using)"
        } else if k.count == 1 {
            script = "tell application \"System Events\" to keystroke \"\(esc(k))\"\(using)"
        } else {
            return ["ok": false, "error": "unsupported key: \(key)"]
        }
        let r = runAppleScript(script)
        if !r.0 { return ["ok": false, "error": r.2] }
        return ["ok": true, "key": k, "modifiers": modifiers, "result": "sent"]
    }

    private func menu(app: String, path: String) -> [String: Any] {
        let ax = Self.ensureAccessibility(prompt: true)
        guard ax["trusted"] as? Bool == true else {
            return [
                "ok": false,
                "error": (ax["error"] as? String)
                    ?? "MacAgent needs Accessibility enabled.",
            ]
        }
        let parts = path.split(separator: ">").map {
            $0.trimmingCharacters(in: .whitespaces)
        }.filter { !$0.isEmpty }
        guard parts.count >= 2 else {
            return ["ok": false, "error": "menu_path must be like 'File > New'"]
        }
        var appName = app
        if appName.isEmpty {
            let front = runAppleScript(
                "tell application \"System Events\" to get name of first process whose frontmost is true"
            )
            guard front.0 else { return ["ok": false, "error": front.2] }
            appName = front.1
        }
        let menuBar = esc(parts[0])
        var body = "click menu bar item \"\(menuBar)\" of menu bar 1\n"
        var current = "menu \"\(menuBar)\" of menu bar item \"\(menuBar)\" of menu bar 1"
        for (i, part) in parts.dropFirst().enumerated() {
            let item = esc(part)
            body += "click menu item \"\(item)\" of \(current)\n"
            if i < parts.count - 2 {
                current = "menu \"\(item)\" of menu item \"\(item)\" of \(current)"
            }
        }
        let script = """
        tell application "System Events"
          tell process "\(esc(appName))"
            set frontmost to true
            \(body)
          end tell
        end tell
        """
        let r = runAppleScript(script)
        if !r.0 { return ["ok": false, "error": r.2, "menu_path": path] }
        return ["ok": true, "app": appName, "menu_path": path, "result": "clicked"]
    }
}
