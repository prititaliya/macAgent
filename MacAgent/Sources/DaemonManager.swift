import Foundation

/// Starts the FastAPI daemon if needed; only kills the process we spawned.
final class DaemonManager {
    private(set) var startedByUs = false
    private var process: Process?
    private let healthURL = URL(string: "http://127.0.0.1:8081/health")!

    var projectRoot: String {
        (Bundle.main.object(forInfoDictionaryKey: "MacAgentRoot") as? String)
            ?? FileManager.default.currentDirectoryPath
    }

    func isHealthy() async -> Bool {
        do {
            let (_, response) = try await URLSession.shared.data(from: healthURL)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }

    @discardableResult
    func ensureRunning() async -> Bool {
        if await isHealthy() {
            return true
        }
        startChild()
        for _ in 0..<40 {
            if await isHealthy() {
                return true
            }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        return await isHealthy()
    }

    private func startChild() {
        if process?.isRunning == true { return }

        let root = projectRoot
        let python = "\(root)/venv/bin/python3"
        let mainPy = "\(root)/main.py"
        let pythonURL: URL
        if FileManager.default.isExecutableFile(atPath: python) {
            pythonURL = URL(fileURLWithPath: python)
        } else {
            pythonURL = URL(fileURLWithPath: "/usr/bin/env")
        }

        let logDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/MacAgent", isDirectory: true)
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)

        let outPath = logDir.appendingPathComponent("daemon.log").path
        let errPath = logDir.appendingPathComponent("daemon.err").path
        FileManager.default.createFile(atPath: outPath, contents: nil)
        FileManager.default.createFile(atPath: errPath, contents: nil)

        let proc = Process()
        proc.currentDirectoryURL = URL(fileURLWithPath: root)
        if pythonURL.path.hasSuffix("python3") || pythonURL.path.contains("/venv/") {
            proc.executableURL = pythonURL
            proc.arguments = [mainPy]
        } else {
            proc.executableURL = pythonURL
            proc.arguments = ["python3", mainPy]
        }
        proc.standardOutput = try? FileHandle(forWritingTo: URL(fileURLWithPath: outPath))
        proc.standardError = try? FileHandle(forWritingTo: URL(fileURLWithPath: errPath))

        do {
            try proc.run()
            process = proc
            startedByUs = true
        } catch {
            startedByUs = false
            process = nil
        }
    }

    func stopIfOwned() {
        guard startedByUs, let proc = process, proc.isRunning else { return }
        proc.terminate()
        // Give it a moment, then force if needed
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
            if proc.isRunning {
                proc.interrupt()
            }
        }
        process = nil
        startedByUs = false
    }
}
