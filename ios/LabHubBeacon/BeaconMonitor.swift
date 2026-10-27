import Foundation
import Combine
import CoreLocation
import UserNotifications

struct BeaconLog: Identifiable, Codable {
    let id: UUID
    let time: String
    let message: String
}

// Core Location の iBeacon 領域監視。LINEアプリからは独立しています。
// 近距離の厳密な距離測定ではなく、iOSによる範囲内／範囲外の推定です。
@MainActor
final class BeaconMonitor: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = BeaconMonitor()

    @Published private(set) var permissionText = "未確認"
    @Published private(set) var statusText = "未確認"
    @Published private(set) var monitoringText = "停止中"
    @Published private(set) var logs: [BeaconLog] = []

    private let locationManager = CLLocationManager()
    private let defaults = UserDefaults.standard
    private let lastStateKey = "labhub.beacon.lastState"
    private let savedLogsKey = "labhub.beacon.logs"

    private override init() {
        super.init()
        locationManager.delegate = self
        if let data = defaults.data(forKey: savedLogsKey),
           let saved = try? JSONDecoder().decode([BeaconLog].self, from: data) {
            logs = saved
        }
        refreshPermissionText()
        if let state = defaults.string(forKey: lastStateKey) {
            statusText = state == "inside" ? "範囲内（前回の観測）" : "範囲外（前回の観測）"
        }
    }

    private var beaconRegion: CLBeaconRegion {
        let region = CLBeaconRegion(
            uuid: BeaconConfig.uuid,
            major: BeaconConfig.major,
            minor: BeaconConfig.minor,
            identifier: BeaconConfig.identifier
        )
        region.notifyOnEntry = true
        region.notifyOnExit = true
        region.notifyEntryStateOnDisplay = true
        return region
    }

    private func refreshPermissionText() {
        switch locationManager.authorizationStatus {
        case .authorizedAlways:
            permissionText = "常に許可"
        case .authorizedWhenInUse:
            permissionText = "使用中のみ（画面外で制限あり）"
        case .denied:
            permissionText = "許可なし（設定で変更してください）"
        case .restricted:
            permissionText = "制限あり"
        case .notDetermined:
            permissionText = "未設定"
        @unknown default:
            permissionText = "不明"
        }
    }

    func askForPermission() {
        locationManager.requestAlwaysAuthorization()
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func startIfAuthorized() {
        refreshPermissionText()
        guard CLLocationManager.isMonitoringAvailable(for: CLBeaconRegion.self) else {
            monitoringText = "このiPhoneでは利用不可"
            appendLog("iBeacon領域監視に対応していません")
            return
        }
        let status = locationManager.authorizationStatus
        guard status == .authorizedAlways || status == .authorizedWhenInUse else {
            monitoringText = "位置情報の許可待ち"
            return
        }
        let alreadyRegistered = locationManager.monitoredRegions.contains {
            $0.identifier == BeaconConfig.identifier
        }
        if !alreadyRegistered {
            locationManager.startMonitoring(for: beaconRegion)
        }
        monitoringText = "監視を登録済み"
    }

    func checkNow() {
        let status = locationManager.authorizationStatus
        guard status == .authorizedAlways || status == .authorizedWhenInUse else {
            appendLog("位置情報の許可が必要です")
            return
        }
        startIfAuthorized()
        locationManager.requestState(for: beaconRegion)
        appendLog("現在の状態を問い合わせました")
    }

    private func observe(_ newState: String, source: String) {
        statusText = newState == "inside" ? "範囲内（推定）" : "範囲外（推定）"
        let previous = defaults.string(forKey: lastStateKey)
        guard previous != newState else {
            appendLog("状態確認：\(statusText)［\(source)］（変化なし）")
            return
        }
        defaults.set(newState, forKey: lastStateKey)
        guard let previous else {
            appendLog("初期状態：\(statusText)［\(source)］")
            return
        }
        let description = newState == "inside" ? "入室（ビーコン範囲内）" : "退室（ビーコン範囲外）"
        appendLog("状態変化：\(previous) → \(newState)［\(source)］")
        showLocalNotification(description)
        EventSender.shared.enqueue(action: newState == "inside" ? "enter" : "exit", trigger: source)
    }

    private func appendLog(_ message: String) {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ja_JP")
        formatter.dateFormat = "MM/dd HH:mm:ss"
        logs.insert(BeaconLog(id: UUID(), time: formatter.string(from: Date()), message: message), at: 0)
        if logs.count > 100 { logs = Array(logs.prefix(100)) }
        defaults.set(try? JSONEncoder().encode(logs), forKey: savedLogsKey)
    }

    // 初回観測は安全のため自動送信しない。必要な場合だけ利用者が明示的に同期する。
    func syncCurrentState() {
        guard let state = defaults.string(forKey: lastStateKey), state == "inside" || state == "outside" else {
            appendLog("現在の状態が未確定です。まず『現在の状態を再確認』してください")
            return
        }
        appendLog("現在の状態を明示的に同期します")
        EventSender.shared.enqueue(action: state == "inside" ? "enter" : "exit", trigger: "manual_sync")
    }

    func clearLogs() {
        logs.removeAll()
        defaults.removeObject(forKey: savedLogsKey)
    }

    private func showLocalNotification(_ title: String) {
        guard defaults.object(forKey: "notifyOnEvent") as? Bool ?? true else { return }
        let content = UNMutableNotificationContent()
        content.title = "LabHub Beacon：\(title)"
        content.body = "iOSがiBeaconの監視状態の変化を検知しました。"
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    // CLLocationManagerのdelegate通知は、初期化したメインキューで処理されます。
    // 非isolated callbackから、明示的にMainActorへ処理を戻します。
    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in
            let monitor = BeaconMonitor.shared
            monitor.refreshPermissionText()
            monitor.appendLog("位置情報の権限：\(monitor.permissionText)")
            monitor.startIfAuthorized()
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didStartMonitoringFor region: CLRegion) {
        Task { @MainActor in
            BeaconMonitor.shared.monitoringText = "監視中"
            BeaconMonitor.shared.checkNow()
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didEnterRegion region: CLRegion) {
        Task { @MainActor in
            BeaconMonitor.shared.observe("inside", source: "didEnterRegion")
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didExitRegion region: CLRegion) {
        Task { @MainActor in
            BeaconMonitor.shared.observe("outside", source: "didExitRegion")
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didDetermineState state: CLRegionState,
        for region: CLRegion
    ) {
        let result: String
        switch state {
        case .inside: result = "inside"
        case .outside: result = "outside"
        default: result = "unknown"
        }
        Task { @MainActor in
            if result == "unknown" {
                BeaconMonitor.shared.statusText = "不明"
                BeaconMonitor.shared.appendLog("iOSがビーコンの状態を判定できませんでした")
            } else {
                BeaconMonitor.shared.observe(result, source: "didDetermineState")
            }
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        monitoringDidFailFor region: CLRegion?,
        withError error: Error
    ) {
        let message = error.localizedDescription
        Task { @MainActor in
            BeaconMonitor.shared.monitoringText = "監視エラー"
            BeaconMonitor.shared.appendLog("監視エラー：\(message)")
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        let message = error.localizedDescription
        Task { @MainActor in
            BeaconMonitor.shared.appendLog("位置情報エラー：\(message)")
        }
    }
}
