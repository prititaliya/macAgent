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
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
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

            HStack(spacing: 8) {
                TextField("Ask or tell MacAgent…", text: $draft)
                    .textFieldStyle(.plain)
                    .font(.system(size: 18, weight: .medium))
                    .focused($focused)
                    .onSubmit { send() }
                    .onChange(of: draft) { _ in onInteract() }
                Button("Send") { send() }
                    .buttonStyle(.borderedProminent)
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
            }
            .padding(12)
            .background(.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
            .contentShape(Rectangle())
            .onTapGesture {
                focused = true
                onInteract()
            }

            if model.busy {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(model.statusLine.isEmpty ? "Working…" : model.statusLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(model.traceSteps) { step in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(step.title)
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                Text(step.body)
                                    .font(.system(size: 12, design: .monospaced))
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .padding(8)
                            .background(.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
                            .id(step.id)
                        }

                        if !model.answer.isEmpty && !model.traceSteps.contains(where: { $0.title == "Answer" }) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Answer")
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                Text(model.answer)
                                    .font(.system(size: 14))
                                    .textSelection(.enabled)
                            }
                        }

                        if !model.sources.isEmpty {
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
                }
                .frame(maxHeight: 320)
                .simultaneousGesture(
                    DragGesture(minimumDistance: 1).onChanged { _ in onInteract() }
                )
                .onHover { hovering in
                    if hovering { onInteract() }
                }
                .onChange(of: model.traceSteps.count) { _ in
                    if let last = model.traceSteps.last {
                        withAnimation {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }

            HStack {
                Circle()
                    .fill(model.daemonOnline ? Color.green : Color.orange)
                    .frame(width: 7, height: 7)
                Text(model.daemonOnline ? "Daemon ready" : "Starting daemon…")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Spacer()
                if autoHideSeconds > 0 {
                    Text("Hides in \(autoHideSeconds)s idle")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                Text("Voice: FreeFlow → :8081")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(18)
        .frame(width: 560, height: 480)
        .background {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .strokeBorder(.white.opacity(0.12), lineWidth: 1)
                )
                .shadow(color: .black.opacity(0.35), radius: 24, y: 12)
        }
        .onAppear {
            Task { await model.refreshHealth() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                focused = true
            }
            onInteract()
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
        onInteract()
        Task { await model.ask(q) }
    }
}
