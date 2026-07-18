import Foundation

/// User-selectable overlay auto-hide timeout (seconds). 0 = never.
enum OverlayAutoHide {
    static let defaultsKey = "overlayAutoHideSeconds"
    static let choices: [(label: String, seconds: Int)] = [
        ("Never", 0),
        ("5 seconds", 5),
        ("10 seconds", 10),
        ("15 seconds", 15),
        ("30 seconds", 30),
        ("60 seconds", 60),
    ]

    static var seconds: Int {
        get {
            if UserDefaults.standard.object(forKey: defaultsKey) == nil {
                return 15
            }
            return UserDefaults.standard.integer(forKey: defaultsKey)
        }
        set {
            UserDefaults.standard.set(newValue, forKey: defaultsKey)
        }
    }
}
