// ==============================================================================
// Project: GAIA Messenger iPhone app
// File:    ContentView.swift
// Author:  Kevin Cazabon (kevin@cazabon.com)
// Version: 1.2.0 / 10/21/2026
//
// Copyright (c) 2026 Kevin Cazabon
//
// This software is released under the MIT License.
// https://opensource.org/licenses/MIT
// ==============================================================================

import Foundation
import SwiftUI
import PhotosUI
import CryptoKit
import CommonCrypto
import Combine
import UserNotifications

// MARK: - E2EE Cryptography Helper
class E2EECrypto {
    let key: SymmetricKey
    private let passphraseData: Data
    private static let staticSalt = "kevins-pa-salt-v1".data(using: .utf8)!
    
    init?(passphrase: String) {
        let trimmed = passphrase.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let passData = trimmed.data(using: .utf8) else { return nil }
        
        self.passphraseData = passData
        guard let derivedKey = Self.deriveKey(passphraseData: passData, salt: Self.staticSalt) else { return nil }
        self.key = derivedKey
    }
    
    private static func deriveKey(passphraseData: Data, salt: Data) -> SymmetricKey? {
        var derivedKeyData = Data(repeating: 0, count: 32)
        
        let status = derivedKeyData.withUnsafeMutableBytes { derivedBytes in
            passphraseData.withUnsafeBytes { passBytes in
                salt.withUnsafeBytes { saltBytes in
                    CCKeyDerivationPBKDF(
                        CCPBKDFAlgorithm(kCCPBKDF2),
                        passBytes.baseAddress?.assumingMemoryBound(to: Int8.self),
                        passphraseData.count,
                        saltBytes.baseAddress?.assumingMemoryBound(to: UInt8.self),
                        salt.count,
                        CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA256),
                        100_000,
                        derivedBytes.baseAddress?.assumingMemoryBound(to: UInt8.self),
                        32
                    )
                }
            }
        }
        
        guard status == kCCSuccess else { return nil }
        return SymmetricKey(data: derivedKeyData)
    }
    
    func encrypt(plaintext: String) -> String? {
        guard let data = plaintext.data(using: .utf8) else { return nil }
        guard let encryptedData = encryptData(data) else { return nil }
        return encryptedData.base64EncodedString()
    }
    
    func encryptData(_ rawData: Data) -> Data? {
        var salt = Data(count: 16)
        let result = salt.withUnsafeMutableBytes {
            SecRandomCopyBytes(kSecRandomDefault, 16, $0.baseAddress!)
        }
        guard result == errSecSuccess else { return nil }
        
        guard let dynamicKey = Self.deriveKey(passphraseData: passphraseData, salt: salt) else { return nil }
        
        do {
            let sealedBox = try AES.GCM.seal(rawData, using: dynamicKey)
            guard let combinedBox = sealedBox.combined else { return nil }
            
            var finalData = Data()
            finalData.append(salt)
            finalData.append(combinedBox)
            return finalData
        } catch {
            return nil
        }
    }
    
    func decrypt(base64Payload: String) -> String? {
        let cleanStr = base64Payload.trimmingCharacters(in: CharacterSet(charactersIn: "\" \n\r"))
        guard let data = Data(base64Encoded: cleanStr) else { return nil }
        guard let decryptedData = decryptData(data) else { return nil }
        return String(data: decryptedData, encoding: .utf8)
    }
    
    func decryptData(_ rawData: Data) -> Data? {
        if rawData.count >= 44 {
            let dynamicSalt = rawData.subdata(in: 0..<16)
            let combinedData = rawData.subdata(in: 16..<rawData.count)
            
            if let dynamicKey = Self.deriveKey(passphraseData: passphraseData, salt: dynamicSalt) {
                do {
                    let sealedBox = try AES.GCM.SealedBox(combined: combinedData)
                    return try AES.GCM.open(sealedBox, using: dynamicKey)
                } catch {}
            }
        }
        
        do {
            let sealedBox = try AES.GCM.SealedBox(combined: rawData)
            return try AES.GCM.open(sealedBox, using: key)
        } catch {}
        
        return nil
    }
}

// MARK: - Data Models
struct NtfyAttachment: Codable {
    let name: String
    let url: String
    let type: String?
    let size: Int?
}

struct NtfyPayload: Codable {
    let id: String
    let time: Int
    let event: String
    let topic: String
    let tags: [String]?
    let message: String?
    let title: String?
    let actions: [NtfyAction]?
    let attachment: NtfyAttachment?
}

struct NtfyAction: Codable, Identifiable, Equatable {
    var id: String { label }
    let action: String
    let label: String
    let url: String?
    let body: String?
}

struct ChatSession: Identifiable, Codable, Equatable {
    let id: String
    var title: String
    let createdAt: Date
    var agentId: String
    var isPinned: Bool
}

struct ChatMessage: Identifiable, Equatable, Codable {
    let id: String
    let sessionId: String
    let text: String
    let isFromUser: Bool
    let timestamp: Date
    let actions: [NtfyAction]?
    let attachmentUrl: String?
    let localImageData: Data?
    
    var isDelivered: Bool
    var pipelineStatus: String // "pending", "received", "processing", "completed"
    
    init(id: String, sessionId: String = "main", text: String, isFromUser: Bool, timestamp: Date, actions: [NtfyAction]? = nil, attachmentUrl: String? = nil, localImageData: Data? = nil, isDelivered: Bool = true, pipelineStatus: String = "completed") {
        self.id = id
        self.sessionId = sessionId
        self.text = text
        self.isFromUser = isFromUser
        self.timestamp = timestamp
        self.actions = actions
        self.attachmentUrl = attachmentUrl
        self.localImageData = localImageData
        self.isDelivered = isDelivered
        self.pipelineStatus = pipelineStatus
    }
}

// MARK: - ViewModel
@MainActor
class NtfyChatViewModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    @Published var sessions: [ChatSession] = []
    @Published var currentSessionId: String = "main"
    @Published var messageText: String = ""
    @Published var isSending: Bool = false
    @Published var connectionError: String? = nil
    @Published var availableAgents: [String] = ["kevins-pa", "system"]
    
    @AppStorage("ntfy_server_url") var serverUrl: String = "https://ntfy.sh"
    @AppStorage("ntfy_topic") var topic: String = "kevins-userver-push-notifications-madcow4242"
    @AppStorage("ntfy_passphrase") var encryptionPassphrase: String = ""
    @AppStorage("ntfy_authorization") var authorizationToken: String = ""
    @AppStorage("ntfy_retention_days") var retentionDays: Int = 7
    @AppStorage("ntfy_target_agent") var targetAgent: String = "kevins-pa" {
        didSet { handleAgentSelectionChanged() }
    }
    
    @AppStorage("ntfy_target_id") var bridgeTargetId: String = "gaia-desktop"
    @AppStorage("ntfy_sender_id") var bridgeSenderId: String = "kevin-ios"
    @AppStorage("ntfy_hmac_secret") var hmacSecret: String = ""
    
    @AppStorage("ntfy_persisted_agents") private var persistedAgentsData: Data = Data()
    @AppStorage("ntfy_persisted_sessions") private var persistedSessionsData: Data = Data()
    
    private var streamTask: Task<Void, Never>?
    
    private var historyFileURL: URL {
        let paths = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)
        return paths[0].appendingPathComponent("chat_history_v2.json")
    }
    
    init() {
        loadAgents()
        loadSessions()
        loadMessagesFromDisk()
        restartListener()
        
        if let active = sessions.first(where: { $0.id == currentSessionId }) {
            targetAgent = active.agentId
        } else if let first = sessions.first {
            currentSessionId = first.id
            targetAgent = first.agentId
        }
        
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }
    
    deinit { streamTask?.cancel() }
    
    var cryptoHelper: E2EECrypto? { E2EECrypto(passphrase: encryptionPassphrase) }
    
    var currentSessionMessages: [ChatMessage] { messages.filter { $0.sessionId == currentSessionId } }
    
    func createAuthenticatedRequest(url: URL, timeout: TimeInterval = 30) -> URLRequest {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: timeout)
        let cleanToken = self.authorizationToken.trimmingCharacters(in: .whitespacesAndNewlines)
        if !cleanToken.isEmpty {
            if cleanToken.lowercased().hasPrefix("bearer ") || cleanToken.lowercased().hasPrefix("basic ") {
                request.setValue(cleanToken, forHTTPHeaderField: "Authorization")
            } else {
                request.setValue("Bearer \(cleanToken)", forHTTPHeaderField: "Authorization")
            }
        }
        return request
    }
    
    // MARK: - Sessions & Agent Management
    func loadAgents() {
        if let decoded = try? JSONDecoder().decode([String].self, from: persistedAgentsData), !decoded.isEmpty {
            self.availableAgents = decoded
        }
        if !availableAgents.contains(targetAgent) { availableAgents.append(targetAgent) }
    }
    
    func saveAgents() {
        if let encoded = try? JSONEncoder().encode(self.availableAgents) {
            self.persistedAgentsData = encoded
        }
    }
    
    func registerDiscoveredAgent(_ agentId: String) {
        let clean = agentId.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !clean.isEmpty, !availableAgents.contains(clean) else { return }
        availableAgents.append(clean)
        saveAgents()
    }
    
    func requestAgentList() { sendMessage(customText: "!agents", isSystemCommand: true) }
    
    func handleAgentSelectionChanged() {
        let cleanAgent = targetAgent.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        if let active = sessions.first(where: { $0.id == currentSessionId }), active.agentId.lowercased() == cleanAgent {
            return
        }
    }
    
    func loadSessions() {
        if let decoded = try? JSONDecoder().decode([ChatSession].self, from: persistedSessionsData), !decoded.isEmpty {
            self.sessions = decoded
        }
        
        // Enforce pinned slots
        if !sessions.contains(where: { $0.id == "main" }) {
            sessions.insert(ChatSession(id: "main", title: "💬 Main Chat", createdAt: Date(), agentId: targetAgent, isPinned: true), at: 0)
        }
        if !sessions.contains(where: { $0.id == "alerts" }) {
            sessions.insert(ChatSession(id: "alerts", title: "🔔 System Alerts", createdAt: Date(), agentId: "system", isPinned: true), at: 1)
        }
        
        saveSessions()
    }
    
    func saveSessions() {
        if let encoded = try? JSONEncoder().encode(self.sessions) {
            self.persistedSessionsData = encoded
        }
    }
    
    func createNewSession() {
        let agent = targetAgent.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        let newId = "sess_\(agent)_\(Int(Date().timeIntervalSince1970))"
        let newSession = ChatSession(
            id: newId,
            title: "New Topic",
            createdAt: Date(),
            agentId: agent,
            isPinned: false
        )
        sessions.append(newSession)
        currentSessionId = newId
        saveSessions()
    }
    
    func deleteSession(_ session: ChatSession) {
        messages.removeAll { $0.sessionId == session.id }
        if session.isPinned {
            saveMessagesToDisk()
            return
        }
        
        sessions.removeAll { $0.id == session.id }
        if currentSessionId == session.id {
            currentSessionId = "main"
            targetAgent = sessions.first(where: { $0.id == "main" })?.agentId ?? "kevins-pa"
        }
        saveSessions()
        saveMessagesToDisk()
    }
    
    // MARK: - Persistence & Notifications
    func loadMessagesFromDisk() {
        guard let data = try? Data(contentsOf: historyFileURL),
              let decoded = try? JSONDecoder().decode([ChatMessage].self, from: data) else { return }
        let cutoff = Date().addingTimeInterval(-Double(retentionDays * 86400))
        self.messages = decoded.filter { $0.timestamp >= cutoff }
        self.messages.sort { $0.timestamp < $1.timestamp }
    }
    
    func saveMessagesToDisk() {
        let cutoff = Date().addingTimeInterval(-Double(retentionDays * 86400))
        let pruned = messages.filter { $0.timestamp >= cutoff }
        if let data = try? JSONEncoder().encode(pruned) {
            try? data.write(to: historyFileURL, options: .atomic)
        }
    }
    
    func scheduleLocalNotification(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.badge = 1
        
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 0.1, repeats: false)
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: trigger)
        UNUserNotificationCenter.current().add(request) { _ in }
    }
    
    // MARK: - Crypto Envelopes (Canonical HMAC-SHA256)
    func createSignedEnvelope(promptText: String, sessionId: String, agentId: String) -> String {
        let nonceBytes = (0..<8).map { _ in UInt8.random(in: 0...255) }
        let nonce = nonceBytes.map { String(format: "%02x", $0) }.joined()
        let ts = Int(Date().timeIntervalSince1970)

        // Swift Dictionary stringification doesn't guarantee order. Build string manually to match python sort_keys=True
        let escapedPrompt = promptText.replacingOccurrences(of: "\"", with: "\\\"").replacingOccurrences(of: "\n", with: "\\n")
        let payloadJson = "{\"agent_id\":\"\(agentId)\",\"agent_type\":\"\(agentId)\",\"session_id\":\"\(sessionId)\",\"text\":\"\(escapedPrompt)\",\"thread_id\":\"\(sessionId)\"}"
        
        let sigBase = "\(nonce)|\(ts)|\(payloadJson)"

        var signatureHex = ""
        let cleanHmac = hmacSecret.trimmingCharacters(in: .whitespacesAndNewlines)
        if !cleanHmac.isEmpty {
            let key = SymmetricKey(data: Data(cleanHmac.utf8))
            let hmac = HMAC<SHA256>.authenticationCode(for: Data(sigBase.utf8), using: key)
            signatureHex = Data(hmac).map { String(format: "%02x", $0) }.joined()
        }

        let envelope: [String: Any] = [
            "target": bridgeTargetId.isEmpty ? "gaia-desktop" : bridgeTargetId,
            "sender": bridgeSenderId.isEmpty ? "kevin-ios" : bridgeSenderId,
            "nonce": nonce,
            "ts": ts,
            "payload": [
                "agent_id": agentId,
                "agent_type": agentId,
                "session_id": sessionId,
                "thread_id": sessionId,
                "text": promptText
            ],
            "sig": signatureHex
        ]

        if let jsonData = try? JSONSerialization.data(withJSONObject: envelope, options: []),
           let jsonString = String(data: jsonData, encoding: .utf8) {
            return jsonString
        }
        return promptText
    }
    
    func restartListener() {
        streamTask?.cancel()
        connectionError = nil
        
        var cleanedServer = serverUrl.trimmingCharacters(in: CharacterSet(charactersIn: " /"))
        let cleanTopic = topic.trimmingCharacters(in: CharacterSet(charactersIn: " /"))
        
        guard !cleanedServer.isEmpty, !cleanTopic.isEmpty else {
            connectionError = "Invalid server URL or topic"
            return
        }
        if !cleanedServer.lowercased().hasPrefix("http://") && !cleanedServer.lowercased().hasPrefix("https://") { cleanedServer = "https://\(cleanedServer)" }
        
        let sinceParam: String
        if let newestTimestamp = messages.map({ $0.timestamp }).max() {
            sinceParam = "\(Int(newestTimestamp.timeIntervalSince1970))"
        } else {
            sinceParam = "\(max(1, retentionDays * 24))h"
        }
        
        let rawUrlString = "\(cleanedServer)/\(cleanTopic)/json?since=\(sinceParam)"
        guard let topicUrl = URL(string: rawUrlString.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? rawUrlString) else {
            connectionError = "Invalid server or topic URL"
            return
        }
        
        let startTime = Date()
        streamTask = Task { await self.listenToTopic(url: topicUrl, startTime: startTime) }
    }
    
    func extractTextFromDecryptedPayload(_ rawString: String) -> String {
        let trimmed = rawString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("{") || trimmed.hasPrefix("[") else { return rawString }
        
        if let data = trimmed.data(using: .utf8),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let payload = json["payload"] as? [String: Any], let envText = payload["text"] as? String { return envText.trimmingCharacters(in: .whitespacesAndNewlines) }
            if let res = json["result"] as? String { return res.trimmingCharacters(in: .whitespacesAndNewlines) }
            if let res = json["response"] as? String { return res.trimmingCharacters(in: .whitespacesAndNewlines) }
            if let out = json["output"] as? String { return out.trimmingCharacters(in: .whitespacesAndNewlines) }
            if let contentStr = json["content"] as? String { return contentStr.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        return rawString
    }

    private func listenToTopic(url: URL, startTime: Date) async {
        var attemptCount = 0
        var retryDelay = 5
        let maxDelay = 300

        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        sessionConfig.urlCache = nil
        let session = URLSession(configuration: sessionConfig)

        while !Task.isCancelled {
            attemptCount += 1
            do {
                let request = createAuthenticatedRequest(url: url, timeout: 86400)
                let (bytes, response) = try await session.bytes(for: request)
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                
                guard statusCode == 200 else {
                    let currentDelay = retryDelay
                    retryDelay = min(retryDelay * 2, maxDelay)
                    for second in stride(from: currentDelay, through: 1, by: -1) {
                        if Task.isCancelled { break }
                        await MainActor.run { self.connectionError = "HTTP \(statusCode) Error. Retrying in \(second)s..." }
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                    continue
                }
                
                attemptCount = 0
                retryDelay = 5
                await MainActor.run { if self.connectionError != nil { self.connectionError = nil } }
                
                for try await line in bytes.lines {
                    if Task.isCancelled { break }
                    
                    if let data = line.data(using: .utf8), let payload = try? JSONDecoder().decode(NtfyPayload.self, from: data), payload.event == "message" {
                        
                        var rawText = payload.message ?? ""
                        var fetchedAttachmentUrl: String? = nil
                        
                        // Handle attachments (text vs binary)
                        if let attachment = payload.attachment, let attachUrl = URL(string: attachment.url) {
                            let attachName = attachment.name.lowercased()
                            let attachType = (attachment.type ?? "").lowercased()
                            let isTextAttachment = attachName.hasSuffix(".txt") || attachName == "attachment" || attachType.hasPrefix("text/")
                                                   
                            if isTextAttachment {
                                var fetchedData: Data? = nil
                                if let (data, resp) = try? await session.data(from: attachUrl), (resp as? HTTPURLResponse)?.statusCode == 200 {
                                    fetchedData = data
                                } else {
                                    let req = self.createAuthenticatedRequest(url: attachUrl)
                                    if let (data, resp) = try? await session.data(for: req), (resp as? HTTPURLResponse)?.statusCode == 200 {
                                        fetchedData = data
                                    }
                                }
                                if let d = fetchedData, let str = String(data: d, encoding: .utf8) {
                                    let trimmed = str.trimmingCharacters(in: .whitespacesAndNewlines)
                                    if !trimmed.isEmpty { rawText = trimmed; fetchedAttachmentUrl = nil }
                                }
                            } else {
                                fetchedAttachmentUrl = attachment.url
                            }
                        }
                        
                        // Decrypt with Plaintext Fallback
                        var displayText: String
                        if let crypto = self.cryptoHelper {
                            let trimmedRaw = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
                            if !trimmedRaw.isEmpty {
                                if let decrypted = crypto.decrypt(base64Payload: trimmedRaw) {
                                    displayText = decrypted
                                } else {
                                    // Fallback to raw text so unencrypted tool alerts aren't dropped
                                    displayText = trimmedRaw
                                }
                            } else if fetchedAttachmentUrl != nil {
                                displayText = ""
                            } else {
                                continue
                            }
                        } else {
                            displayText = rawText
                            if displayText.isEmpty && fetchedAttachmentUrl == nil { continue }
                        }
                        
                        // Intercept Pipeline ACKs
                        if displayText.hasPrefix("{") {
                            if let json = try? JSONSerialization.jsonObject(with: displayText.data(using: .utf8)!) as? [String: Any],
                               let ackData = json["ack"] as? [String: Any],
                               let refId = ackData["ref_id"] as? String,
                               let status = ackData["status"] as? String {
                                await MainActor.run {
                                    if let idx = self.messages.firstIndex(where: { $0.id == refId }) {
                                        self.messages[idx].pipelineStatus = status
                                        self.saveMessagesToDisk()
                                    }
                                }
                                continue
                            }
                        }
                        
                        // Intercept System Commands
                        if displayText.hasPrefix("[AGENTS_LIST]:") {
                            let jsonPart = displayText.replacingOccurrences(of: "[AGENTS_LIST]:", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
                            if let jsonData = jsonPart.data(using: .utf8), let list = try? JSONDecoder().decode([String].self, from: jsonData) {
                                await MainActor.run { for agent in list { self.registerDiscoveredAgent(agent) } }
                            }
                            continue
                        }
                        if displayText.hasPrefix("[SYSTEM_COMMAND:") || displayText.hasPrefix("[SYSTEM:") { continue }
                        
                        displayText = extractTextFromDecryptedPayload(displayText)
                        if displayText.hasPrefix("You received a file:") { displayText = "⚠️ Unable to load attachment content." }
                        
                        // Extract Routing Headers / Tags
                        var msgThreadId: String? = nil
                        var msgAgentId: String? = nil
                        var senderId = ""
                        let tags = payload.tags ?? []
                        
                        for tag in tags {
                            if tag.hasPrefix("tid:") { msgThreadId = String(tag.dropFirst(4)) }
                            else if tag.hasPrefix("sid:") { msgThreadId = String(tag.dropFirst(4)) }
                            else if tag.hasPrefix("agent:") { msgAgentId = String(tag.dropFirst(6)) }
                            else if tag.hasPrefix("client:") { senderId = String(tag.dropFirst(7)) }
                        }
                        
                        let title = payload.title ?? ""
                        let hasClientTag = tags.contains { $0.hasPrefix("client:") }
                        
                        // Origin Guard: Only messages explicitly bearing client tags are user messages
                        let isUser = hasClientTag
                        
                        // Drop sibling client echoes
                        if isUser && !senderId.isEmpty && senderId != self.bridgeSenderId {
                            continue
                        }
                        
                        // Resolve Thread: User defaults to "main", Agent notifications default to "alerts"
                        let effectiveThreadId = msgThreadId ?? (isUser ? "main" : "alerts")
                        let msgTime = Date(timeIntervalSince1970: TimeInterval(payload.time))
                        
                        await MainActor.run {
                            // Ensure Target Thread Exists
                            if let idx = self.sessions.firstIndex(where: { $0.id == effectiveThreadId }) {
                                if !self.sessions[idx].isPinned { 
                                    self.sessions[idx].agentId = msgAgentId ?? self.sessions[idx].agentId 
                                }
                                self.saveSessions()
                            } else {
                                let titleStr = (effectiveThreadId == "alerts") ? "🔔 System Alerts" : "# \(effectiveThreadId)"
                                let s = ChatSession(
                                    id: effectiveThreadId,
                                    title: titleStr,
                                    createdAt: msgTime,
                                    agentId: msgAgentId ?? self.targetAgent,
                                    isPinned: (effectiveThreadId == "main" || effectiveThreadId == "alerts")
                                )
                                self.sessions.append(s)
                                self.saveSessions()
                            }
                            
                            // Deduplicate and insert
                            if let localIndex = self.messages.firstIndex(where: {
                                $0.isFromUser && ($0.id == payload.id || ($0.id.hasPrefix("temp-") && ($0.text.trimmingCharacters(in: .whitespacesAndNewlines) == displayText.trimmingCharacters(in: .whitespacesAndNewlines) || ($0.localImageData != nil && fetchedAttachmentUrl != nil))))
                            }) {
                                var updated = self.messages[localIndex]
                                updated = ChatMessage(
                                    id: payload.id,
                                    sessionId: effectiveThreadId,
                                    text: displayText.isEmpty ? updated.text : displayText,
                                    isFromUser: true,
                                    timestamp: msgTime,
                                    actions: payload.actions,
                                    attachmentUrl: fetchedAttachmentUrl ?? updated.attachmentUrl,
                                    localImageData: updated.localImageData,
                                    isDelivered: true,
                                    pipelineStatus: "received"
                                )
                                self.messages[localIndex] = updated
                                self.saveMessagesToDisk()
                            } else if !self.messages.contains(where: { $0.id == payload.id }) {
                                let msg = ChatMessage(
                                    id: payload.id,
                                    sessionId: effectiveThreadId,
                                    text: displayText,
                                    isFromUser: isUser,
                                    timestamp: msgTime,
                                    actions: payload.actions,
                                    attachmentUrl: fetchedAttachmentUrl,
                                    isDelivered: true,
                                    pipelineStatus: "completed"
                                )
                                self.messages.append(msg)
                                self.messages.sort { $0.timestamp < $1.timestamp }
                                
                                // Auto-complete preceding user prompt if agent replied
                                if !isUser {
                                    if let lastUserIdx = self.messages.lastIndex(where: { $0.isFromUser && $0.sessionId == effectiveThreadId }) {
                                        self.messages[lastUserIdx].pipelineStatus = "completed"
                                    }
                                }
                                self.saveMessagesToDisk()
                                
                                if msgTime >= startTime.addingTimeInterval(-5) && !isUser {
                                    let alertTitle = title.isEmpty ? "GAIA (@\(msgAgentId ?? self.targetAgent))" : title
                                    self.scheduleLocalNotification(
                                        title: alertTitle,
                                        body: displayText.isEmpty ? "Sent an attachment" : displayText
                                    )
                                }
                            }
                        }
                    }
                }
            } catch {
                if !Task.isCancelled {
                    let currentDelay = retryDelay
                    retryDelay = min(retryDelay * 2, maxDelay)
                    for second in stride(from: currentDelay, through: 1, by: -1) {
                        if Task.isCancelled { break }
                        await MainActor.run { self.connectionError = "Connection error. Retrying in \(second)s..." }
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                }
            }
        }
    }
    
    @MainActor
    func sendMessage(customText: String? = nil, attachmentData: Data? = nil, fileName: String? = nil, isSystemCommand: Bool = false) {
        var textToSend = (customText ?? messageText).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !textToSend.isEmpty || attachmentData != nil else { return }
        
        let cleanTopic = topic.trimmingCharacters(in: CharacterSet(charactersIn: " /"))
        guard !cleanTopic.isEmpty else {
            self.connectionError = "Topic name cannot be empty."
            return
        }
        
        let cleanAgent = targetAgent.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if !isSystemCommand && !textToSend.isEmpty && !textToSend.hasPrefix("@") && !cleanAgent.isEmpty {
            textToSend = "@\(cleanAgent) \(textToSend)"
        }
        
        isSending = true
        let tempId = "temp-\(UUID().uuidString)"
        
        if !isSystemCommand {
            if let activeIdx = sessions.firstIndex(where: { $0.id == currentSessionId }) {
                if sessions[activeIdx].title == "New Topic" && !textToSend.isEmpty && !sessions[activeIdx].isPinned {
                    let rawContent = textToSend.replacingOccurrences(of: "@\(cleanAgent) ", with: "")
                    sessions[activeIdx].title = "# " + String(rawContent.prefix(28)) + (rawContent.count > 28 ? "…" : "")
                    saveSessions()
                }
            }
            
            let localMessage = ChatMessage(id: tempId, sessionId: currentSessionId, text: textToSend, isFromUser: true, timestamp: Date(), actions: nil, attachmentUrl: nil, localImageData: attachmentData, isDelivered: false, pipelineStatus: "pending")
            self.messages.append(localMessage)
            saveMessagesToDisk()
        }
        
        Task {
            defer { Task { @MainActor in self.isSending = false } }
            
            let cleanedServer = serverUrl.trimmingCharacters(in: CharacterSet(charactersIn: " /"))
            guard let url = URL(string: "\(cleanedServer)/\(cleanTopic)") else {
                await MainActor.run { self.connectionError = "Invalid server or topic URL" }
                return
            }
            
            var request = createAuthenticatedRequest(url: url, timeout: 20.0)
            let signedEnvelopeString = createSignedEnvelope(promptText: textToSend, sessionId: currentSessionId, agentId: cleanAgent.isEmpty ? "kevins-pa" : cleanAgent)
            
            let payloadText: String
            if let crypto = self.cryptoHelper, let encrypted = crypto.encrypt(plaintext: signedEnvelopeString) {
                payloadText = encrypted
            } else {
                payloadText = signedEnvelopeString
            }
            
            // X-Routing Headers
            request.setValue("client", forHTTPHeaderField: "X-Sender-Type")
            request.setValue(bridgeSenderId.isEmpty ? "kevin-ios" : bridgeSenderId, forHTTPHeaderField: "X-Sender-ID")
            request.setValue(bridgeTargetId.isEmpty ? "gaia-desktop" : bridgeTargetId, forHTTPHeaderField: "X-Target-Server")
            request.setValue(bridgeSenderId.isEmpty ? "kevin-ios" : bridgeSenderId, forHTTPHeaderField: "X-Target-Client")
            request.setValue(cleanAgent, forHTTPHeaderField: "X-Agent-ID")
            request.setValue(currentSessionId, forHTTPHeaderField: "X-Thread-ID")
            request.setValue("chat", forHTTPHeaderField: "X-Message-Type")
            request.setValue("tid:\(currentSessionId),agent:\(cleanAgent),client:\(bridgeSenderId)", forHTTPHeaderField: "Tags")
            request.setValue("4", forHTTPHeaderField: "Priority")
            
            if let data = attachmentData {
                let uploadName = fileName ?? "enc_photo.bin"
                request.httpMethod = "PUT"
                request.setValue(uploadName.hasPrefix("enc_") ? uploadName : "enc_\(uploadName)", forHTTPHeaderField: "Filename")
                
                if !payloadText.isEmpty { request.setValue(payloadText.components(separatedBy: .newlines).joined(separator: " "), forHTTPHeaderField: "Message") }
                
                if let crypto = self.cryptoHelper, let encryptedBlob = crypto.encryptData(data) { request.httpBody = encryptedBlob }
                else { request.httpBody = data }
            } else {
                request.httpMethod = "POST"
                request.setValue("User Prompt", forHTTPHeaderField: "Title")
                request.httpBody = payloadText.data(using: .utf8)
            }
            
            do {
                let (responseData, response) = try await URLSession.shared.data(for: request)
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                
                if statusCode == 200 {
                    if !isSystemCommand, let resJson = try? JSONDecoder().decode(NtfyPayload.self, from: responseData) {
                        await MainActor.run {
                            if let idx = self.messages.firstIndex(where: { $0.id == tempId }) {
                                var msg = self.messages[idx]
                                msg = ChatMessage(id: resJson.id, sessionId: msg.sessionId, text: msg.text, isFromUser: true, timestamp: msg.timestamp, actions: msg.actions, attachmentUrl: msg.attachmentUrl, localImageData: msg.localImageData, isDelivered: true, pipelineStatus: "received")
                                self.messages[idx] = msg
                                self.saveMessagesToDisk()
                            }
                        }
                    }
                    await MainActor.run { self.connectionError = nil }
                } else {
                    let errorBody = String(data: responseData, encoding: .utf8) ?? ""
                    await MainActor.run { self.connectionError = "Server HTTP \(statusCode): \(errorBody.prefix(60))" }
                }
            } catch {
                await MainActor.run { self.connectionError = "Network error. Message queued locally." }
            }
        }
    }
}

// MARK: - Main View
struct ContentView: View {
    @StateObject private var viewModel = NtfyChatViewModel()
    @State private var showSettings = false
    @State private var showHistorySheet = false
    @Environment(\.scenePhase) var scenePhase
    
    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if let errorMsg = viewModel.connectionError {
                    HStack(spacing: 6) {
                        Image(systemName: "wifi.slash").font(.caption)
                        Text(errorMsg).font(.caption).fontWeight(.medium)
                        Spacer()
                        Button(action: { viewModel.restartListener() }) {
                            Text("Retry").font(.caption2).fontWeight(.bold).padding(.horizontal, 8).padding(.vertical, 3).background(Color.orange).foregroundColor(.white).cornerRadius(6)
                        }
                    }
                    .padding(.horizontal, 12).padding(.vertical, 6).background(Color.orange.opacity(0.15)).foregroundColor(.orange)
                    .transition(.move(edge: .top).combined(with: .opacity))
                }
                
                HStack(spacing: 10) {
                    HStack(spacing: 4) {
                        Image(systemName: "cpu").font(.caption).foregroundColor(.blue)
                        Picker("Agent", selection: $viewModel.targetAgent) {
                            ForEach(viewModel.availableAgents, id: \.self) { agent in Text("@\(agent)").tag(agent) }
                        }.pickerStyle(.menu).font(.caption)
                    }
                    .padding(.horizontal, 8).padding(.vertical, 4).background(Color(.systemGray6)).cornerRadius(10)
                    
                    Spacer()
                    
                    Menu {
                        Button(action: { viewModel.createNewSession() }) { Label("New Topic", systemImage: "plus.bubble") }
                        Button(action: { showHistorySheet = true }) { Label("Switch Thread", systemImage: "list.bullet") }
                        Button(action: { viewModel.requestAgentList() }) { Label("Refresh Agents", systemImage: "arrow.clockwise") }
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "bubble.left.and.bubble.right").font(.caption)
                            Text(viewModel.sessions.first(where: { $0.id == viewModel.currentSessionId })?.title ?? "Threads")
                                .font(.caption).lineLimit(1)
                            Image(systemName: "chevron.down").font(.caption2)
                        }
                        .padding(.horizontal, 10).padding(.vertical, 6).background(Color.blue.opacity(0.12)).foregroundColor(.blue).cornerRadius(10)
                    }
                }
                .padding(.horizontal, 12).padding(.vertical, 6).background(Color(.systemBackground))
                
                Divider()
                
                ChatAreaView(viewModel: viewModel)
            }
            .navigationTitle("GAIA Messenger")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) { Button(action: { showSettings = true }) { Image(systemName: "gearshape") } }
            }
            .sheet(isPresented: $showSettings, onDismiss: { viewModel.restartListener() }) { SettingsView(viewModel: viewModel) }
            .sheet(isPresented: $showHistorySheet) { SessionHistoryView(viewModel: viewModel) }
        }
        .onChange(of: scenePhase) { oldPhase, newPhase in
            if newPhase == .active { UNUserNotificationCenter.current().setBadgeCount(0) }
        }
    }
}

// MARK: - Chat Area View
struct ChatAreaView: View {
    @ObservedObject var viewModel: NtfyChatViewModel
    @State private var selectedPhotoItem: PhotosPickerItem?
    @State private var stagedImageData: Data? = nil
    @State private var inputBuffer: String = ""

    private func scrollToBottom(proxy: ScrollViewProxy, animated: Bool = true) {
        guard let lastId = viewModel.currentSessionMessages.last?.id else { return }
        if animated { withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo(lastId, anchor: .bottom) } }
        else { proxy.scrollTo(lastId, anchor: .bottom) }
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(viewModel.currentSessionMessages) { msg in
                            ChatBubble(message: msg, passphrase: viewModel.encryptionPassphrase, authToken: viewModel.authorizationToken, onActionTap: { bodyPayload in
                                viewModel.sendMessage(customText: bodyPayload)
                            })
                            .id(msg.id)
                        }
                    }
                    .padding()
                }
                .onAppear { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { scrollToBottom(proxy: proxy, animated: false) } }
                .onChange(of: viewModel.currentSessionId) { _, _ in DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { scrollToBottom(proxy: proxy, animated: false) } }
                .onChange(of: viewModel.currentSessionMessages.last?.id) { _, _ in scrollToBottom(proxy: proxy, animated: true) }
            }
            
            Divider()
            
            if let data = stagedImageData, let uiImage = UIImage(data: data) {
                HStack(alignment: .center, spacing: 10) {
                    ZStack(alignment: .topTrailing) {
                        Image(uiImage: uiImage).resizable().aspectRatio(contentMode: .fill).frame(width: 56, height: 56).cornerRadius(10).clipped()
                        Button(action: { stagedImageData = nil; selectedPhotoItem = nil }) {
                            Image(systemName: "xmark.circle.fill").font(.system(size: 18)).foregroundColor(.white).background(Circle().fill(Color.black.opacity(0.6)))
                        }.offset(x: 6, y: -6)
                    }
                    .padding(.top, 6)
                    Text("Photo attached").font(.caption).foregroundColor(.secondary)
                    Spacer()
                }
                .padding(.horizontal).padding(.bottom, 4).background(Color(.systemBackground))
            }
            
            HStack(spacing: 8) {
                PhotosPicker(selection: $selectedPhotoItem, matching: .images) {
                    Image(systemName: stagedImageData != nil ? "paperclip.circle.fill" : "paperclip").font(.system(size: 20)).foregroundColor(stagedImageData != nil ? .blue : .gray)
                }
                .onChange(of: selectedPhotoItem) { _, newItem in
                    Task { if let data = try? await newItem?.loadTransferable(type: Data.self) { await MainActor.run { self.stagedImageData = data } } }
                }
                
                TextField("Message @\(viewModel.targetAgent)...", text: $inputBuffer)
                    .padding(10).background(Color(.systemGray6)).cornerRadius(20)
                
                Button(action: {
                    let imageToSend = stagedImageData
                    let capturedText = inputBuffer
                    inputBuffer = ""
                    stagedImageData = nil
                    selectedPhotoItem = nil
                    Task { @MainActor in
                        viewModel.messageText = capturedText
                        viewModel.sendMessage(customText: capturedText, attachmentData: imageToSend, fileName: "enc_photo.bin")
                    }
                }) {
                    Image(systemName: "arrow.up.circle.fill").resizable().frame(width: 32, height: 32).foregroundColor((inputBuffer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && stagedImageData == nil) ? .gray : .blue)
                }
                .disabled((inputBuffer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && stagedImageData == nil) || viewModel.isSending)
            }
            .padding(.horizontal).padding(.vertical, 8).background(Color(.systemBackground))
        }
    }
}

// MARK: - Session History Modal View
struct SessionHistoryView: View {
    @ObservedObject var viewModel: NtfyChatViewModel
    @Environment(\.dismiss) private var dismiss
    
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button(action: { viewModel.createNewSession(); dismiss() }) {
                        Label("Start New Topic", systemImage: "plus.circle.fill").foregroundColor(.blue)
                    }
                }
                
                Section(header: Text("Core Channels")) {
                    ForEach(viewModel.sessions.filter { $0.isPinned }) { session in
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(session.title).fontWeight(viewModel.currentSessionId == session.id ? .bold : .regular)
                                HStack {
                                    Text("@\(session.agentId)").font(.caption2).padding(3).background(Color(.systemGray6)).cornerRadius(4)
                                    Text("Permanent").font(.caption2).foregroundColor(.blue)
                                }
                            }
                            Spacer()
                            if viewModel.currentSessionId == session.id { Image(systemName: "checkmark.circle.fill").foregroundColor(.blue) }
                        }
                        .contentShape(Rectangle())
                        .onTapGesture { viewModel.currentSessionId = session.id; viewModel.targetAgent = session.agentId; dismiss() }
                    }
                }
                
                Section(header: Text("Topics & Tasks")) {
                    ForEach(viewModel.sessions.filter { !$0.isPinned }) { session in
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(session.title).fontWeight(viewModel.currentSessionId == session.id ? .bold : .regular)
                                HStack {
                                    Text("@\(session.agentId)").font(.caption2).padding(3).background(Color(.systemGray6)).cornerRadius(4)
                                    Text("\(session.createdAt, style: .date)").font(.caption2).foregroundColor(.secondary)
                                }
                            }
                            Spacer()
                            if viewModel.currentSessionId == session.id { Image(systemName: "checkmark.circle.fill").foregroundColor(.blue) }
                        }
                        .contentShape(Rectangle())
                        .onTapGesture { viewModel.currentSessionId = session.id; viewModel.targetAgent = session.agentId; dismiss() }
                    }
                    .onDelete { indexSet in
                        let unpinned = viewModel.sessions.filter { !$0.isPinned }
                        for index in indexSet { viewModel.deleteSession(unpinned[index]) }
                    }
                }
            }
            .navigationTitle("Threads & Slots")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .navigationBarTrailing) { Button("Done") { dismiss() } } }
        }
    }
}

// MARK: - Encrypted Image Attachment View
struct EncryptedImageView: View {
    let urlString: String
    let passphrase: String
    let authToken: String
    
    @State private var image: UIImage? = nil
    @State private var isLoading: Bool = true
    @State private var errorMessage: String? = nil
    @State private var showFullScreen: Bool = false
    
    var body: some View {
        Group {
            if let uiImage = image {
                Image(uiImage: uiImage).resizable().aspectRatio(contentMode: .fit).frame(maxWidth: 240, maxHeight: 240).cornerRadius(12).contentShape(Rectangle())
                    .onTapGesture { showFullScreen = true }
                    .fullScreenCover(isPresented: $showFullScreen) { FullScreenImageViewer(uiImage: uiImage) }
            } else if isLoading {
                ProgressView().frame(width: 120, height: 120).background(Color(.systemGray6)).cornerRadius(12)
            } else {
                Label(errorMessage ?? "Unavailable", systemImage: "wifi.exclamationmark").font(.caption).foregroundColor(.orange).padding(8).background(Color(.systemGray6)).cornerRadius(8)
            }
        }
        .task { await loadAndDecryptImage() }
    }
    
    private func loadAndDecryptImage() async {
        guard let url = URL(string: urlString) else { return }
        do {
            var request = URLRequest(url: url, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 10.0)
            let trimmedToken = authToken.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmedToken.isEmpty { request.setValue("Bearer \(trimmedToken)", forHTTPHeaderField: "Authorization") }
            
            let (encData, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                errorMessage = "Download Error"; isLoading = false; return
            }
            
            if let crypto = E2EECrypto(passphrase: passphrase), let decryptedData = crypto.decryptData(encData), let decodedImage = UIImage(data: decryptedData) {
                self.image = decodedImage
            } else if let unencryptedImage = UIImage(data: encData) {
                self.image = unencryptedImage
            } else { errorMessage = "Invalid Format" }
        } catch { errorMessage = "Image Unavailable" }
        isLoading = false
    }
}

// MARK: - Full Screen Viewer Modal
struct FullScreenImageViewer: View {
    let uiImage: UIImage
    @Environment(\.dismiss) private var dismiss
    @State private var showShareSheet: Bool = false
    
    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()
                Image(uiImage: uiImage).resizable().aspectRatio(contentMode: .fit)
            }
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button(action: { showShareSheet = true }) { Image(systemName: "square.and.arrow.up").foregroundColor(.white) }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button(action: { dismiss() }) { Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(.white) }
                }
            }
            .sheet(isPresented: $showShareSheet) { ActivityViewController(activityItems: [uiImage]) }
        }
    }
}

struct ActivityViewController: UIViewControllerRepresentable {
    var activityItems: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController { UIActivityViewController(activityItems: activityItems, applicationActivities: nil) }
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}

// MARK: - Chat Bubble
struct ChatBubble: View {
    let message: ChatMessage
    let passphrase: String
    let authToken: String
    let onActionTap: (String) -> Void
    @State private var showFullScreenLocal: Bool = false
    
    private var pipelineIcon: (String, Color) {
        switch message.pipelineStatus {
        case "completed": return ("checkmark.circle.fill", .blue)
        case "processing": return ("brain", .blue)
        case "received": return ("checkmark.circle", .blue)
        default: return ("clock", .gray)
        }
    }
    
    var body: some View {
        HStack {
            if message.isFromUser { Spacer() }
            
            VStack(alignment: message.isFromUser ? .trailing : .leading, spacing: 6) {
                if let localData = message.localImageData, let uiImage = UIImage(data: localData) {
                    Image(uiImage: uiImage).resizable().aspectRatio(contentMode: .fit).frame(maxWidth: 240, maxHeight: 240).cornerRadius(12).contentShape(Rectangle())
                        .onTapGesture { showFullScreenLocal = true }
                        .fullScreenCover(isPresented: $showFullScreenLocal) { FullScreenImageViewer(uiImage: uiImage) }
                } else if let urlString = message.attachmentUrl {
                    EncryptedImageView(urlString: urlString, passphrase: passphrase, authToken: authToken)
                }
                
                if !message.text.isEmpty && message.text != "Sent an attachment" {
                    Text(message.text)
                        .padding(.horizontal, 14).padding(.vertical, 10).background(message.isFromUser ? Color.blue : Color(.systemGray5)).foregroundColor(message.isFromUser ? .white : .primary).cornerRadius(18)
                }
                
                if let actions = message.actions, !actions.isEmpty {
                    HStack(spacing: 8) {
                        ForEach(actions) { act in
                            Button(action: { if let body = act.body { onActionTap(body) } }) {
                                Text(act.label).font(.caption).fontWeight(.bold).padding(.horizontal, 12).padding(.vertical, 6).background(Color.blue.opacity(0.15)).foregroundColor(.blue).cornerRadius(12)
                            }
                        }
                    }
                }
                
                HStack(spacing: 4) {
                    Text(message.timestamp, style: .time).font(.caption2).foregroundColor(.gray)
                    if message.isFromUser {
                        Image(systemName: pipelineIcon.0).font(.system(size: 10)).foregroundColor(pipelineIcon.1)
                    }
                }
                .padding(.horizontal, 4)
            }
            if !message.isFromUser { Spacer() }
        }
    }
}

// MARK: - Settings View
struct SettingsView: View {
    @ObservedObject var viewModel: NtfyChatViewModel
    @Environment(\.dismiss) private var dismiss
    
    var body: some View {
        NavigationStack {
            Form {
                Section(header: Text("Routing Identities")) {
                    TextField("Target Server ID", text: $viewModel.bridgeTargetId).autocapitalization(.none).disableAutocorrection(true).foregroundColor(.secondary)
                    TextField("This Client ID", text: $viewModel.bridgeSenderId).autocapitalization(.none).disableAutocorrection(true).foregroundColor(.secondary)
                }
                Section(header: Text("Server Configuration")) {
                    TextField("Server URL", text: $viewModel.serverUrl).autocapitalization(.none).disableAutocorrection(true).keyboardType(.URL)
                    SecureField("Authorization Token (Optional)", text: $viewModel.authorizationToken).autocapitalization(.none).disableAutocorrection(true)
                }
                Section(header: Text("NTFY Channel")) {
                    TextField("NTFY Topic Name", text: $viewModel.topic).autocapitalization(.none).disableAutocorrection(true)
                }
                Section(header: Text("Bridge Security"), footer: Text("HMAC Secret verifies sender identity. E2EE Passphrase encrypts payloads with AES-256-GCM.")) {
                    SecureField("HMAC Secret (Signatures)", text: $viewModel.hmacSecret).autocapitalization(.none).disableAutocorrection(true)
                    SecureField("E2EE Passphrase (Encryption)", text: $viewModel.encryptionPassphrase).autocapitalization(.none).disableAutocorrection(true)
                }
                Section(header: Text("Local Storage & Retention")) {
                    Picker("History Retention", selection: $viewModel.retentionDays) {
                        Text("1 Day").tag(1); Text("3 Days").tag(3); Text("7 Days").tag(7); Text("14 Days").tag(14); Text("30 Days").tag(30)
                    }
                }
            }
            .navigationTitle("Routing & Security").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .navigationBarTrailing) { Button("Done") { dismiss() }.fontWeight(.bold) } }
        }
    }
}