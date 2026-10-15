// ==============================================================================
// Project: GAIA Messenger iPhone app
// File:    GAIA_MessengerApp.swift
// Author:  Kevin Cazabon (kevin@cazabon.com)
// Version: 1.1.0 / 10/13/2026
//
// Copyright (c) 2026 Kevin Cazabon
//
// This software is released under the MIT License.
// https://opensource.org/licenses/MIT
// ==============================================================================

import SwiftUI
import UserNotifications

class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    
    func application(
        _ application: UIApplication, 
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey : Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }
    
    // MARK: - APNs Device Token Registration
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let tokenParts = deviceToken.map { data in String(format: "%02.2hhx", data) }
        let tokenString = tokenParts.joined()
        
        UserDefaults.standard.set(tokenString, forKey: "ntfy_apns_device_token")
        
        // Forward token to upstream ntfy relay
        GAIA_MessengerApp.subscribeToNtfyAPNs(deviceToken: tokenString)
    }
    
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        print("[APNs] Failed to register for remote notifications: \(error.localizedDescription)")
    }
    
    // MARK: - Background Remote Push / Silent Notification Wakes
    func application(
        _ application: UIApplication, 
        didReceiveRemoteNotification userInfo: [AnyHashable : Any], 
        fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
    ) {
        // Increment badge on incoming remote push
        UNUserNotificationCenter.current().setBadgeCount(1)
        completionHandler(.newData)
    }
    
    // MARK: - Foreground Presentation (Plays sound & banner even if app is foregrounded)
    func userNotificationCenter(
        _ center: UNUserNotificationCenter, 
        willPresent notification: UNNotification, 
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        // Present banner, sound, and badge so user gets audio feedback when agent finishes
        completionHandler([.banner, .sound, .badge])
    }
    
    // MARK: - Notification Tap (Deep link to active session)
    func userNotificationCenter(
        _ center: UNUserNotificationCenter, 
        didReceive response: UNNotificationResponse, 
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        
        // Extract session ID if embedded in ntfy click/tag payload
        if let clickUrl = userInfo["click"] as? String, let url = URL(string: clickUrl) {
            if let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
               let sidItem = components.queryItems?.first(where: { $0.name == "session_id" })?.value {
                UserDefaults.standard.set(sidItem, forKey: "ntfy_active_session_id")
            }
        }
        
        completionHandler()
    }
}

@main
struct GAIA_MessengerApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @Environment(\.scenePhase) var scenePhase
    
    var body: some Scene {
        WindowGroup {
            ContentView()
                .onAppear {
                    requestNotificationPermission()
                }
        }
        .onChange(of: scenePhase) { oldPhase, newPhase in
            if newPhase == .active {
                // Clear app badge when foregrounded
                UNUserNotificationCenter.current().setBadgeCount(0)
            }
        }
    }
    
    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if granted {
                DispatchQueue.main.async {
                    UIApplication.shared.registerForRemoteNotifications()
                    
                    // Attempt subscribe with cached token if already present
                    if let cachedToken = UserDefaults.standard.string(forKey: "ntfy_apns_device_token") {
                        Self.subscribeToNtfyAPNs(deviceToken: cachedToken)
                    }
                }
            }
        }
    }
    
    // MARK: - APNs Subscription Bridge
    static func subscribeToNtfyAPNs(deviceToken: String) {
        let topic = UserDefaults.standard.string(forKey: "ntfy_topic") ?? "kevins-userver-push-notifications-madcow4242"
        var serverUrl = UserDefaults.standard.string(forKey: "ntfy_server_url") ?? "https://ntfy.sh"
        let authToken = UserDefaults.standard.string(forKey: "ntfy_authorization") ?? ""
        
        let cleanTopic = topic.trimmingCharacters(in: CharacterSet(charactersIn: " /"))
        var cleanedServer = serverUrl.trimmingCharacters(in: CharacterSet(charactersIn: " /"))
        
        guard !cleanTopic.isEmpty else { return }
        if !cleanedServer.lowercased().hasPrefix("http") {
            cleanedServer = "https://\(cleanedServer)"
        }
        
        guard let url = URL(string: "\(cleanedServer)/\(cleanTopic)/subscribe") else { return }
        
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 15.0)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let cleanToken = authToken.trimmingCharacters(in: .whitespacesAndNewlines)
        if !cleanToken.isEmpty {
            if cleanToken.lowercased().hasPrefix("bearer ") || cleanToken.lowercased().hasPrefix("basic ") {
                request.setValue(cleanToken, forHTTPHeaderField: "Authorization")
            } else {
                request.setValue("Bearer \(cleanToken)", forHTTPHeaderField: "Authorization")
            }
        }
        
        let payload: [String: Any] = [
            "topic": cleanTopic,
            "device_token": deviceToken,
            "platform": "ios"
        ]
        
        if let data = try? JSONSerialization.data(withJSONObject: payload) {
            request.httpBody = data
            URLSession.shared.dataTask(with: request).resume()
        }
    }
}