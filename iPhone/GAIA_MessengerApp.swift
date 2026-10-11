// ==============================================================================
// Project: GAIA Messenger iPhone app
// File:    GAIA_MessengerApp.swift
// Author:  Kevin Cazabon (kevin@cazabon.com)
// Version: 1.0.0 / 10/11/2026
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
        // Set delegate to handle foreground notification policies
        UNUserNotificationCenter.current().delegate = self
        return true
    }
    
    // Delegate method controlling foreground behavior
    func userNotificationCenter(
        _ center: UNUserNotificationCenter, 
        willPresent notification: UNNotification, 
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        // Suppress banners and sounds while user is actively looking at the chat
        completionHandler([])
    }
}

@main
struct GAIA_MessengerApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @Environment(\.scenePhase) var scenePhase
    
    // Read the topic from UserDefaults to subscribe to APNs
    @AppStorage("ntfy_topic") var topic: String = "kevins-userver-push-notifications-madcow4242"
    @AppStorage("ntfy_server_url") var serverUrl: String = "https://ntfy.sh"
    
    var body: some Scene {
        WindowGroup {
            ContentView()
                .onAppear {
                    requestNotificationPermission()
                }
        }
        .onChange(of: scenePhase) { oldPhase, newPhase in
            if newPhase == .active {
                // Clear the red badge when the user opens the app
                UNUserNotificationCenter.current().setBadgeCount(0)
            }
        }
    }
    
    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if granted {
                DispatchQueue.main.async {
                    UIApplication.shared.registerForRemoteNotifications()
                    subscribeToNtfyAPNs()
                }
            }
        }
    }
    
    // Subscribes the device to ntfy's native Apple Push Notification Service (APNs)
    private func subscribeToNtfyAPNs() {
        let cleanTopic = topic.trimmingCharacters(in: CharacterSet(charactersIn: " /"))
        var cleanedServer = serverUrl.trimmingCharacters(in: CharacterSet(charactersIn: " /"))
        
        guard !cleanTopic.isEmpty else { return }
        if !cleanedServer.lowercased().hasPrefix("http") {
            cleanedServer = "https://\(cleanedServer)"
        }
        
        // This endpoint tells the ntfy server to route messages for this topic to APNs for this app ID
        // Note: For ntfy APNs to work in your own app, you must either be using a self-hosted ntfy server 
        // with APNs configured for your developer account, or rely on local notifications.
        // If this fails, the local notifications scheduled by ContentView's stream listener will act as the fallback.
        if let url = URL(string: "\(cleanedServer)/\(cleanTopic)/subscribe") {
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            
            // Note: Replace with your actual device token logic if using a custom APNs backend
            let payload: [String: Any] = [
                "topic": cleanTopic
            ]
            
            if let data = try? JSONSerialization.data(withJSONObject: payload) {
                request.httpBody = data
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                
                URLSession.shared.dataTask(with: request).resume()
            }
        }
    }
}