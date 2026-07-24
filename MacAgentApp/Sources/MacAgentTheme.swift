import SwiftUI

/// Single source of truth for MacAgent's visual language.
///
/// Direction: one confident **teal** accent (on-device / calm / not the generic
/// AI purple), a warm **amber** reserved strictly for permission + auto-hide
/// urgency, atmospheric material instead of flat gray, and restrained motion.
enum Theme {
    /// Primary accent — a deep, slightly desaturated teal. Reads well on both the
    /// translucent overlay and the light Preferences window.
    static let accent = Color(red: 0.09, green: 0.67, blue: 0.61)
    /// Deeper teal for text/among-light surfaces where the bright accent is too faint.
    static let accentDeep = Color(red: 0.04, green: 0.49, blue: 0.46)
    /// Softer teal used for glows and gradient washes.
    static let accentGlow = Color(red: 0.20, green: 0.80, blue: 0.72)

    /// Reserved for permission prompts + the final auto-hide countdown. Never used
    /// as a general accent, so its appearance always means "attention".
    static let caution = Color(red: 0.97, green: 0.64, blue: 0.25)
    static let cautionDeep = Color(red: 0.85, green: 0.47, blue: 0.10)

    static let positive = Color(red: 0.30, green: 0.78, blue: 0.55)
    static let danger = Color(red: 0.94, green: 0.36, blue: 0.36)

    // Corner radii — a small, consistent scale.
    static let radiusPanel: CGFloat = 20
    static let radiusCard: CGFloat = 14
    static let radiusControl: CGFloat = 12
    static let radiusChip: CGFloat = 9
}

// MARK: - Typography

extension Font {
    /// Brand wordmark — SF Pro Rounded conveys the friendly-but-precise HUD feel.
    static func brand(_ size: CGFloat, _ weight: Font.Weight = .semibold) -> Font {
        .system(size: size, weight: weight, design: .rounded)
    }
    /// Tabular numerals for countdowns / metrics.
    static func metric(_ size: CGFloat, _ weight: Font.Weight = .semibold) -> Font {
        .system(size: size, weight: weight, design: .rounded)
    }
}

// MARK: - Atmospheric overlay background

/// The floating HUD surface: ultraThinMaterial base + a single faint accent wash
/// in the top-leading corner so the panel feels lit, not flat. Optional caution
/// state swaps the wash + border for the amber "about to hide" look — one clean
/// signal instead of layered neon.
struct OverlaySurface: View {
    var urgent: Bool
    var urgentPulse: Bool

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Theme.radiusPanel, style: .continuous)
        shape
            .fill(.ultraThinMaterial)
            .overlay {
                shape.fill(
                    LinearGradient(
                        colors: [
                            (urgent ? Theme.caution : Theme.accentGlow).opacity(urgent ? 0.16 : 0.12),
                            .clear
                        ],
                        startPoint: .topLeading,
                        endPoint: .center
                    )
                )
            }
            .overlay {
                shape.strokeBorder(
                    borderStyle,
                    lineWidth: urgent ? 1.5 : 1
                )
            }
            .shadow(
                color: urgent
                    ? Theme.caution.opacity(urgentPulse ? 0.32 : 0.18)
                    : Color.black.opacity(0.32),
                radius: urgent ? (urgentPulse ? 22 : 16) : 26,
                y: 14
            )
    }

    private var borderStyle: AnyShapeStyle {
        if urgent {
            return AnyShapeStyle(Theme.caution.opacity(urgentPulse ? 0.9 : 0.5))
        }
        return AnyShapeStyle(
            LinearGradient(
                colors: [Color.white.opacity(0.22), Color.white.opacity(0.06)],
                startPoint: .top,
                endPoint: .bottom
            )
        )
    }
}

// MARK: - Chip (overlay option pills)

/// Restyled Model / Search chips: a single subtle capsule, no neon, with a clear
/// trailing chevron so they read as menus.
struct OverlayChip: View {
    let systemImage: String
    let title: String
    var value: String? = nil
    var active: Bool = false

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: systemImage)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(active ? Theme.accent : Color.secondary)
            Text(title)
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(.primary.opacity(0.85))
            if let value {
                Text(value)
                    .font(.system(size: 11.5, weight: .semibold))
                    .foregroundStyle(active ? Theme.accent : .primary.opacity(0.7))
            }
            Image(systemName: "chevron.down")
                .font(.system(size: 8, weight: .bold))
                .foregroundStyle(.tertiary)
        }
        .lineLimit(1)
        .padding(.horizontal, 11)
        .padding(.vertical, 6)
        .background(
            Capsule(style: .continuous)
                .fill(Color.primary.opacity(0.06))
        )
        .overlay(
            Capsule(style: .continuous)
                .strokeBorder(
                    active ? Theme.accent.opacity(0.45) : Color.white.opacity(0.10),
                    lineWidth: 1
                )
        )
        .contentShape(Capsule())
    }
}

// MARK: - Buttons

/// Filled accent button (send, primary confirm actions).
struct AccentButtonStyle: ButtonStyle {
    var tint: Color = Theme.accent
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(
                Capsule(style: .continuous)
                    .fill(tint.opacity(configuration.isPressed ? 0.8 : 1))
            )
            .contentShape(Capsule())
            .opacity(configuration.isPressed ? 0.9 : 1)
    }
}

/// Quiet bordered button (deny, secondary actions).
struct GhostButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .medium))
            .foregroundStyle(.primary.opacity(0.85))
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(
                Capsule(style: .continuous)
                    .fill(Color.primary.opacity(configuration.isPressed ? 0.10 : 0.06))
            )
            .overlay(
                Capsule(style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.12), lineWidth: 1)
            )
            .contentShape(Capsule())
    }
}

/// Small square icon button used for secondary header controls.
struct IconControlButtonStyle: ButtonStyle {
    var active: Bool = false
    var activeColor: Color = Theme.accent
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12.5, weight: .semibold))
            .foregroundStyle(active ? activeColor : Color.secondary)
            .frame(width: 26, height: 26)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(
                        active
                            ? activeColor.opacity(0.16)
                            : Color.primary.opacity(configuration.isPressed ? 0.12 : 0.05)
                    )
            )
            .contentShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
    }
}

// MARK: - Brand mark

/// Small rounded tile holding the logo (or a fallback glyph) with a soft accent
/// backing — gives the header a real identity anchor.
struct BrandMark: View {
    var size: CGFloat = 30

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
            .fill(
                LinearGradient(
                    colors: [Theme.accent, Theme.accentDeep],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
            )
            .overlay {
                logoImage
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .padding(size * 0.16)
                    .clipShape(RoundedRectangle(cornerRadius: size * 0.18, style: .continuous))
            }
            .overlay(
                RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.18), lineWidth: 0.5)
            )
            .frame(width: size, height: size)
            .shadow(color: Theme.accent.opacity(0.35), radius: 6, y: 2)
    }

    private var logoImage: Image {
        if let ns = NSImage(named: "Logo") {
            return Image(nsImage: ns)
        }
        if let url = Bundle.main.url(forResource: "MacAgentLogo", withExtension: "png"),
           let ns = NSImage(contentsOf: url) {
            return Image(nsImage: ns)
        }
        return Image(systemName: "sparkle")
    }
}

// MARK: - Preferences chrome

/// Atmospheric backdrop for Preferences detail pages — a very subtle top wash so
/// the window doesn't read as flat gray, tuned to be quiet under content.
struct PrefsBackground: View {
    var body: some View {
        ZStack {
            Color(nsColor: .windowBackgroundColor)
            LinearGradient(
                colors: [Theme.accent.opacity(0.06), .clear],
                startPoint: .top,
                endPoint: .center
            )
            .frame(height: 260)
            .frame(maxHeight: .infinity, alignment: .top)
        }
        .ignoresSafeArea()
    }
}

/// Section label used above grouped cards in Preferences.
struct PrefsSectionLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 11, weight: .semibold))
            .tracking(0.6)
            .foregroundStyle(.secondary)
    }
}

// MARK: - Markdown body

/// Renders model answers with Markdown (bold, italic, code, links).
/// Falls back to a lightweight inline converter if Foundation's parser fails —
/// so `**value**` never shows raw asterisks in the overlay.
struct MarkdownBody: View {
    let text: String
    var fontSize: CGFloat = 15

    init(_ text: String, fontSize: CGFloat = 15) {
        self.text = text
        self.fontSize = fontSize
    }

    var body: some View {
        // Do not apply `.font()` here — it flattens Markdown bold/italic runs.
        Text(attributed)
            .textSelection(.enabled)
            .tint(Theme.accentDeep)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var attributed: AttributedString {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return AttributedString("") }

        if let parsed = Self.parseMarkdown(trimmed, fontSize: fontSize) {
            return parsed
        }
        return Self.inlineFallback(trimmed, fontSize: fontSize)
    }

    private static func parseMarkdown(_ raw: String, fontSize: CGFloat) -> AttributedString? {
        var options = AttributedString.MarkdownParsingOptions()
        options.interpretedSyntax = .inlineOnlyPreservingWhitespace
        if let parsed = try? AttributedString(markdown: raw, options: options) {
            return withBaseFont(parsed, fontSize: fontSize)
        }
        options.interpretedSyntax = .full
        if let parsed = try? AttributedString(markdown: raw, options: options) {
            return withBaseFont(parsed, fontSize: fontSize)
        }
        return nil
    }

    private static func withBaseFont(_ input: AttributedString, fontSize: CGFloat) -> AttributedString {
        var result = input
        let base = Font.system(size: fontSize, weight: .regular)
        for run in result.runs {
            guard run.font == nil else { continue }
            var attrs = AttributeContainer()
            attrs.font = base
            result[run.range].mergeAttributes(attrs, mergePolicy: .keepCurrent)
        }
        if result.runs.allSatisfy({ $0.font == nil }) {
            result.font = base
        }
        return result
    }

    /// Manual `**bold**` / `*italic*` / `` `code` `` so asterisks never leak.
    private static func inlineFallback(_ raw: String, fontSize: CGFloat) -> AttributedString {
        let baseFont = Font.system(size: fontSize, weight: .regular)
        let boldFont = Font.system(size: fontSize, weight: .semibold)
        let italicFont = Font.system(size: fontSize, weight: .regular).italic()
        let monoFont = Font.system(size: fontSize - 1, weight: .regular, design: .monospaced)

        var output = AttributedString()
        var i = raw.startIndex

        while i < raw.endIndex {
            if raw[i] == "`",
               let close = raw[raw.index(after: i)...].firstIndex(of: "`"),
               close > raw.index(after: i)
            {
                let inner = raw[raw.index(after: i)..<close]
                var chunk = AttributedString(String(inner))
                chunk.font = monoFont
                chunk.backgroundColor = Color.primary.opacity(0.08)
                output.append(chunk)
                i = raw.index(after: close)
                continue
            }

            if matchesDelimiter(raw, at: i, delimiter: "**")
                || matchesDelimiter(raw, at: i, delimiter: "__")
            {
                let delim = raw[i] == "*" ? "**" : "__"
                if let (inner, end) = enclosed(raw, at: i, delimiter: delim) {
                    var chunk = AttributedString(inner)
                    chunk.font = boldFont
                    output.append(chunk)
                    i = end
                    continue
                }
            }

            if (raw[i] == "*" || raw[i] == "_"),
               !matchesDelimiter(raw, at: i, delimiter: "**"),
               !matchesDelimiter(raw, at: i, delimiter: "__")
            {
                let delim = String(raw[i])
                if let (inner, end) = enclosed(raw, at: i, delimiter: delim) {
                    var chunk = AttributedString(inner)
                    chunk.font = italicFont
                    output.append(chunk)
                    i = end
                    continue
                }
            }

            var plain = AttributedString(String(raw[i]))
            plain.font = baseFont
            output.append(plain)
            i = raw.index(after: i)
        }

        if output.characters.isEmpty {
            var plain = AttributedString(raw)
            plain.font = baseFont
            return plain
        }
        return output
    }

    private static func matchesDelimiter(_ s: String, at i: String.Index, delimiter: String) -> Bool {
        guard let end = s.index(i, offsetBy: delimiter.count, limitedBy: s.endIndex) else {
            return false
        }
        return s[i..<end] == delimiter
    }

    private static func enclosed(
        _ s: String,
        at start: String.Index,
        delimiter: String
    ) -> (String, String.Index)? {
        guard matchesDelimiter(s, at: start, delimiter: delimiter) else { return nil }
        let contentStart = s.index(start, offsetBy: delimiter.count)
        guard contentStart < s.endIndex else { return nil }
        var search = contentStart
        while let found = s[search...].range(of: delimiter)?.lowerBound {
            if found > contentStart {
                let inner = String(s[contentStart..<found])
                if !inner.isEmpty, !inner.contains("\n") {
                    let end = s.index(found, offsetBy: delimiter.count)
                    return (inner, end)
                }
            }
            search = s.index(after: found)
        }
        return nil
    }
}
