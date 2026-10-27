import SwiftUI
import CoreLocation
import UIKit

// Raspberry Pi が送信する LabHub 専用 iBeacon と一致させます。
enum BeaconConfig {
    static let uuid = UUID(uuidString: "C4A814CF-5E48-4D16-A652-0F391BD74E05")!
    static let major: CLBeaconMajorValue = 1
    static let minor: CLBeaconMinorValue = 1
    static let identifier = "labhub.lab1.room1"
}

// OS がアプリを起動した場合も、できるだけ早く監視オブジェクトを準備します。
@MainActor
final class LabHubAppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        BeaconMonitor.shared.startIfAuthorized()
        EventSender.shared.flush()
        return true
    }
}

@main
struct LabHubBeaconApp: App {
    @UIApplicationDelegateAdaptor(LabHubAppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(BeaconMonitor.shared)
        }
    }
}
