import SwiftUI
import AppKit

struct OverlayView: View {
    @ObservedObject var model: AgentModel
    var onDismiss: () -> Void
    var onPrefs: () -> Void
    var onQuit: () -> Void = { AppDelegate.shared?.quitApp() }
    var onInteract: () -> Void = {}
    @AppStorage(OverlayAutoHide.defaultsKey) private var autoHideSeconds: Int = 15
    @State private var draft = ""
    @State private var logsExpanded = false
    @FocusState private var focused: Bool
    @StateObject private var speech = SpeechCapture()

    private var logSteps: [TraceStep] {
        model.traceSteps.filter { $0.title != "Answer" && $0.title != "Input" }
    }

    private var hasAnswer: Bool {
        !model.answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var countdownLabel: String {
        guard let left = model.hideCountdown, autoHideSeconds > 0 else {
            return autoHideSeconds == 0 ? "Auto-hide off" : ""
        }
        if model.busy || model.pendingConfirm != nil {
            return "Paused · \(left)s"
        }
        return "Hides in \(left)s"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            inputBar

            if model.busy {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(model.statusLine.isEmpty ? "Working…" : model.statusLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else if speech.isListening {
                HStack(spacing: 8) {
                    TimelineView(.animation(minimumInterval: 0.45, paused: false)) { context in
                        Circle()
                            .fill(Color.red)
                            .frame(width: 7, height: 7)
                            .opacity(
                                Int(context.date.timeIntervalSinceReferenceDate * 2) % 2 == 0
                                    ? 1.0 : 0.25
                            )
                    }
                    Text(speech.statusMessage.isEmpty ? "Listening…" : speech.statusMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else if !speech.statusMessage.isEmpty {
                Text(speech.statusMessage)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if !model.lastQuestion.isEmpty {
                        Text(model.lastQuestion)
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }

                    if let pending = model.pendingConfirm {
                        VStack(alignment: .leading, spacing: 10) {
                            Label("Permission needed", systemImage: "hand.raised.fill")
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(.orange)
                            Text(pending.summary)
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(.primary)
                                .textSelection(.enabled)
                            if !pending.command.isEmpty {
                                Text(pending.command)
                                    .font(.system(size: 11, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                            }
                            HStack(spacing: 10) {
                                Button("Deny") {
                                    onInteract()
                                    Task { await model.respondToConfirm(approve: false) }
                                }
                                .buttonStyle(.bordered)
                                Button("Approve") {
                                    onInteract()
                                    Task { await model.respondToConfirm(approve: true) }
                                }
                                .buttonStyle(.borderedProminent)
                                .tint(.orange)
                            }
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 14, style: .continuous)
                                .fill(Color.orange.opacity(0.14))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 14, style: .continuous)
                                .strokeBorder(Color.orange.opacity(0.5), lineWidth: 1.5)
                        )
                    }

                    // Final answer — visually distinct
                    if hasAnswer {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("Answer", systemImage: "checkmark.seal.fill")
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(Color.accentColor)
                            Text(model.answer)
                                .font(.system(size: 16, weight: .semibold))
                                .foregroundStyle(.primary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 14, style: .continuous)
                                .fill(Color.accentColor.opacity(0.14))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 14, style: .continuous)
                                .strokeBorder(Color.accentColor.opacity(0.45), lineWidth: 1.5)
                        )
                    }

                    if !model.sources.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Sources")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                            ForEach(model.sources) { src in
                                Button {
                                    onInteract()
                                    model.openURL(src.url)
                                } label: {
                                    Text(src.title.isEmpty ? src.url : src.title)
                                        .font(.caption)
                                        .foregroundStyle(.blue)
                                        .lineLimit(2)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }

                    // Collapsible activity logs
                    if !logSteps.isEmpty || model.busy {
                        DisclosureGroup(isExpanded: $logsExpanded) {
                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(logSteps) { step in
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(step.title)
                                            .font(.caption2.weight(.semibold))
                                            .foregroundStyle(.tertiary)
                                        Text(step.body)
                                            .font(.system(size: 11, design: .monospaced))
                                            .foregroundStyle(.secondary)
                                            .textSelection(.enabled)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                    .padding(8)
                                    .background(.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
                                }
                            }
                            .padding(.top, 6)
                        } label: {
                            HStack {
                                Image(systemName: "chevron.right.circle")
                                    .rotationEffect(.degrees(logsExpanded ? 90 : 0))
                                Text(logsExpanded ? "Hide activity logs" : "Show activity logs")
                                    .font(.caption.weight(.medium))
                                Text("(\(logSteps.count))")
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                                Spacer()
                            }
                            .foregroundStyle(.secondary)
                            .contentShape(Rectangle())
                        }
                        .onChange(of: hasAnswer) { answered in
                            if answered {
                                withAnimation(.easeInOut(duration: 0.2)) {
                                    logsExpanded = false
                                }
                            }
                        }
                        .onChange(of: model.busy) { busy in
                            // While working, keep logs open so progress is visible.
                            if busy {
                                logsExpanded = true
                            }
                        }
                    }
                }
            }
            .simultaneousGesture(
                DragGesture(minimumDistance: 1).onChanged { _ in onInteract() }
            )

            footer
        }
        .padding(18)
        .frame(minWidth: 420, minHeight: 280)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .strokeBorder(borderColor, lineWidth: model.hideUrgency ? 2.5 : 1)
                )
                .shadow(
                    color: model.hideUrgency
                        ? Color.orange.opacity(model.hidePulse ? 0.55 : 0.18)
                        : Color.black.opacity(0.35),
                    radius: model.hideUrgency ? (model.hidePulse ? 26 : 14) : 24,
                    y: 12
                )
        }
        // Slight scale only while urgent + pulse beat — no forever animation.
        .scaleEffect(model.hideUrgency && model.hidePulse ? 1.015 : 1.0)
        .animation(.easeInOut(duration: 0.2), value: model.hidePulse)
        .onAppear {
            Task { await model.refreshHealth() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                focused = true
            }
            onInteract()
            logsExpanded = !hasAnswer
        }
        .onChange(of: model.lastQuestion) { _ in
            if model.busy || !hasAnswer {
                logsExpanded = true
            }
        }
        .onChange(of: autoHideSeconds) { _ in
            onInteract()
        }
        .onChange(of: model.isDictating) { dictating in
            if !dictating && speech.isListening {
                speech.cancel()
            }
        }
    }

    private var borderColor: Color {
        if model.hideUrgency {
            return Color.orange.opacity(model.hidePulse ? 0.95 : 0.4)
        }
        return Color.white.opacity(0.12)
    }

    private var header: some View {
        HStack(spacing: 8) {
            logoImage
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(width: 28, height: 28)
                .clipShape(Circle())
            Text("MacAgent")
                .font(.system(size: 13, weight: .semibold, design: .rounded))
                .foregroundStyle(.secondary)
            Spacer()
            Text("⌃⌥Space")
                .font(.caption2.monospaced())
                .foregroundStyle(.tertiary)
            Button(action: onPrefs) {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Preferences")
            Button(action: onDismiss) {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Hide overlay")
            .keyboardShortcut(.escape, modifiers: [])
            Button(action: onQuit) {
                Image(systemName: "power")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Quit MacAgent")
        }
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            Button {
                onInteract()
                Task { await toggleMic() }
            } label: {
                Image(systemName: speech.isListening ? "mic.fill" : "mic")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(speech.isListening ? Color.red : Color.secondary)
                    .frame(width: 28, height: 28)
                    .background(
                        Circle()
                            .fill(speech.isListening ? Color.red.opacity(0.18) : Color.white.opacity(0.06))
                    )
            }
            .buttonStyle(.plain)
            .help(speech.isListening ? "Stop listening" : "Dictate with Mac speech recognition")
            .disabled(model.busy && !speech.isListening)
            .accessibilityLabel(speech.isListening ? "Stop dictation" : "Start dictation")

            TextField(
                speech.isListening ? "Listening… tap mic to send" : "Ask or tell MacAgent…",
                text: $draft
            )
                .textFieldStyle(.plain)
                .font(.system(size: 18, weight: .medium))
                .focused($focused)
                .onSubmit { send() }
                .onChange(of: draft) { _ in onInteract() }
                .onChange(of: speech.partialText) { partial in
                    if speech.isListening, !partial.isEmpty {
                        draft = partial
                        onInteract()
                    }
                }

            Button(speech.isListening ? "Stop" : "Send") {
                if speech.isListening {
                    Task { await toggleMic() }
                } else {
                    send()
                }
            }
                .buttonStyle(.borderedProminent)
                .tint(speech.isListening ? .red : nil)
                .disabled(
                    speech.isListening
                        ? false
                        : (draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
                )
        }
        .padding(12)
        .background(.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(speech.isListening ? Color.red.opacity(0.45) : Color.clear, lineWidth: 1.5)
        )
        .contentShape(Rectangle())
        .onTapGesture {
            focused = true
            onInteract()
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(model.daemonOnline ? Color.green : Color.orange)
                .frame(width: 7, height: 7)
            Text(model.daemonOnline ? "Daemon ready" : "Starting daemon…")
                .font(.caption2)
                .foregroundStyle(.tertiary)
            Spacer()
            if autoHideSeconds > 0, let left = model.hideCountdown {
                HStack(spacing: 6) {
                    if model.hideUrgency {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .opacity(model.hidePulse ? 1 : 0.35)
                    }
                    Text(countdownLabel)
                        .font(.system(size: 11, weight: model.hideUrgency ? .bold : .medium, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(model.hideUrgency ? Color.orange : Color.secondary)
                    // Tiny progress bar for remaining idle time.
                    Capsule()
                        .fill(Color.white.opacity(0.12))
                        .frame(width: 44, height: 4)
                        .overlay(alignment: .leading) {
                            Capsule()
                                .fill(model.hideUrgency ? Color.orange : Color.accentColor.opacity(0.8))
                                .frame(
                                    width: max(4, 44 * CGFloat(left) / CGFloat(max(autoHideSeconds, 1))),
                                    height: 4
                                )
                        }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(
                    Capsule()
                        .fill(model.hideUrgency ? Color.orange.opacity(0.18) : Color.white.opacity(0.06))
                )
            } else if autoHideSeconds == 0 {
                Text("Auto-hide off")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Text(speech.isListening ? "Mic listening" : "Voice: Mic or FreeFlow → :8081")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
    }

    private var logoImage: Image {
        if let ns = NSImage(named: "Logo") {
            return Image(nsImage: ns)
        }
        if let url = Bundle.main.url(forResource: "MacAgentLogo", withExtension: "png"),
           let ns = NSImage(contentsOf: url) {
            return Image(nsImage: ns)
        }
        return Image(systemName: "sparkles")
    }

    private func send() {
        let q = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        draft = ""
        logsExpanded = true
        onInteract()
        Task { await model.ask(q) }
    }

    private func toggleMic() async {
        onInteract()
        if speech.isListening {
            model.isDictating = false
            let text = speech.stop()
            if !text.isEmpty {
                draft = text
                send()
            }
        } else {
            model.isDictating = true
            await speech.start()
            if !speech.isListening {
                model.isDictating = false
            }
            onInteract()
        }
    }
}
