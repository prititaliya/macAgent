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
                connection.cancel()
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

    private static func extractHTTPBody(_ data: Data) -> Data? {
        let needle: [UInt8] = [13, 10, 13, 10]
        guard data.count >= 4 else { return nil }
        let bytes = [UInt8](data)
        if bytes.count >= 4 {
            for i in 0...(bytes.count - 4) {
                if bytes[i] == 13, bytes[i + 1] == 10, bytes[i + 2] == 13, bytes[i + 3] == 10 {
                    return Data(bytes[(i + 4)...])
                }
            }
        }
        _ = needle
        return nil
    }

    private func dispatch(_ data: Data) -> [String: Any] {
        guard
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let op = obj["op"] as? String
        else {
            return ["ok": false, "error": "invalid request"]
        }

        return DispatchQueue.main.sync {
            switch op {
            case "ping":
                return ["ok": true, "trusted": AXIsProcessTrusted()]
            case "snapshot":
                return snapshot(limit: (obj["limit"] as? Int) ?? 40)
            case "click":
                return click(
                    name: (obj["name"] as? String) ?? "",
                    role: (obj["role"] as? String) ?? "button",
                    index: (obj["index"] as? Int) ?? 1
                )
            case "type":
                return typeText((obj["text"] as? String) ?? "")
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
        guard AXIsProcessTrusted() else {
            return ["ok": false, "error": "MacAgent needs Accessibility enabled for MacAgent.app."]
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
        guard AXIsProcessTrusted() else {
            return ["ok": false, "error": "MacAgent needs Accessibility enabled."]
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

    private func typeText(_ text: String) -> [String: Any] {
        guard AXIsProcessTrusted() else {
            return ["ok": false, "error": "MacAgent needs Accessibility enabled."]
        }
        let r = runAppleScript("""
        tell application "System Events"
          keystroke "\(esc(text))"
        end tell
        """)
        if !r.0 { return ["ok": false, "error": r.2] }
        return ["ok": true, "typed_chars": text.count, "result": "typed"]
    }

    private func keyStroke(key: String, modifiers: String) -> [String: Any] {
        guard AXIsProcessTrusted() else {
            return ["ok": false, "error": "MacAgent needs Accessibility enabled."]
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
        guard AXIsProcessTrusted() else {
            return ["ok": false, "error": "MacAgent needs Accessibility enabled."]
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
