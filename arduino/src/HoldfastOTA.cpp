#include "HoldfastOTA.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Update.h>
#include <WiFiClientSecure.h>
#include <esp_ota_ops.h>
#include <mbedtls/sha256.h>

#include <memory>
#include <new>
#include <utility>

namespace holdfast {
namespace {

bool validArtifactName(const String& name) {
  if (name.isEmpty() || name.startsWith(".") || name.indexOf('/') >= 0 ||
      name.indexOf('\\') >= 0 || name.indexOf("..") >= 0) {
    return false;
  }
  for (size_t i = 0; i < name.length(); ++i) {
    const char ch = name.charAt(i);
    if (!isalnum(static_cast<unsigned char>(ch)) && ch != '.' && ch != '_' && ch != '-') {
      return false;
    }
  }
  return true;
}

String digestHex(const uint8_t digest[32]) {
  static constexpr char HEX[] = "0123456789abcdef";
  String value;
  value.reserve(64);
  for (size_t i = 0; i < 32; ++i) {
    value += HEX[digest[i] >> 4];
    value += HEX[digest[i] & 0x0f];
  }
  return value;
}

}  // namespace

OtaUpdater::OtaUpdater(const OtaConfig& config) : config_(config) {}

void OtaUpdater::onStatus(StatusCallback callback) {
  statusCallback_ = std::move(callback);
}

void OtaUpdater::onService(ServiceCallback callback) {
  serviceCallback_ = std::move(callback);
}

const OtaManifest& OtaUpdater::manifest() const {
  return manifest_;
}

const String& OtaUpdater::lastError() const {
  return lastError_;
}

void OtaUpdater::status(const char* phase, const String& message) {
  if (statusCallback_) statusCallback_(phase, message.c_str());
}

void OtaUpdater::service() {
  if (serviceCallback_) serviceCallback_();
  yield();
}

OtaResult OtaUpdater::fail(OtaResult result, const String& message) {
  lastError_ = message;
  status("failed", message);
  return result;
}

bool OtaUpdater::validConfiguration() {
  if (!config_.baseUrl || !config_.baseUrl[0]) {
    lastError_ = "base URL is required";
    return false;
  }
  if (!config_.target || !config_.target[0]) {
    lastError_ = "target is required";
    return false;
  }
  if (!config_.rootCa || !config_.rootCa[0]) {
    lastError_ = "root CA is required";
    return false;
  }
  if (config_.downloadBufferBytes < 512) {
    lastError_ = "download buffer must be at least 512 bytes";
    return false;
  }
  return true;
}

void OtaUpdater::applyAuthorization(HTTPClient& http) {
  if (config_.authToken && config_.authToken[0]) {
    http.addHeader("Authorization", String("Bearer ") + config_.authToken);
  }
}

bool OtaUpdater::fetchManifest() {
  WiFiClientSecure client;
  client.setCACert(config_.rootCa);
  client.setTimeout(config_.requestTimeoutMs / 1000);

  HTTPClient http;
  http.setTimeout(config_.requestTimeoutMs);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  const String url = String(config_.baseUrl) + "/manifest";
  if (!http.begin(client, url)) {
    lastError_ = "could not open manifest URL";
    return false;
  }
  applyAuthorization(http);

  const int code = http.GET();
  if (code != HTTP_CODE_OK) {
    lastError_ = String("manifest HTTP ") + code;
    http.end();
    return false;
  }

  JsonDocument document;
  const DeserializationError error = deserializeJson(document, http.getStream());
  http.end();
  if (error) {
    lastError_ = String("manifest JSON: ") + error.c_str();
    return false;
  }

  manifest_.version = document["version"] | 0;
  manifest_.target = document["target"] | "";
  manifest_.file = document["firmware"]["file"] | "";
  manifest_.size = document["firmware"]["size"] | 0;
  manifest_.sha256 = document["firmware"]["sha256"] | "";
  manifest_.sha256.toLowerCase();

  if (manifest_.version == 0 || manifest_.target.isEmpty() ||
      !validArtifactName(manifest_.file) || manifest_.size == 0 ||
      manifest_.sha256.length() != 64) {
    lastError_ = "manifest is missing required firmware metadata";
    return false;
  }
  for (size_t i = 0; i < manifest_.sha256.length(); ++i) {
    if (!isxdigit(static_cast<unsigned char>(manifest_.sha256.charAt(i)))) {
      lastError_ = "manifest SHA-256 is invalid";
      return false;
    }
  }
  return true;
}

OtaResult OtaUpdater::downloadAndInstall() {
  WiFiClientSecure client;
  client.setCACert(config_.rootCa);
  client.setTimeout(config_.requestTimeoutMs / 1000);

  HTTPClient http;
  http.setTimeout(config_.requestTimeoutMs);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  const String url = String(config_.baseUrl) + "/files/" + manifest_.file;
  if (!http.begin(client, url)) {
    return fail(OtaResult::DownloadFailed, "could not open firmware URL");
  }
  applyAuthorization(http);

  const int code = http.GET();
  if (code != HTTP_CODE_OK) {
    http.end();
    return fail(OtaResult::DownloadFailed, String("firmware HTTP ") + code);
  }

  const int contentLength = http.getSize();
  if (contentLength < 0 || static_cast<size_t>(contentLength) != manifest_.size) {
    http.end();
    return fail(OtaResult::VerificationFailed, "firmware Content-Length mismatch");
  }
  if (!Update.begin(manifest_.size, U_FLASH)) {
    http.end();
    return fail(OtaResult::InstallFailed, String("Update.begin: ") + Update.errorString());
  }

  std::unique_ptr<uint8_t[]> buffer(new (std::nothrow) uint8_t[config_.downloadBufferBytes]);
  if (!buffer) {
    Update.abort();
    http.end();
    return fail(OtaResult::InstallFailed, "could not allocate download buffer");
  }

  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  mbedtls_sha256_starts_ret(&sha, 0);

  WiFiClient* stream = http.getStreamPtr();
  size_t received = 0;
  unsigned long lastProgressAt = millis();
  status("downloading", String(manifest_.size) + " bytes");

  while (received < manifest_.size) {
    const size_t available = stream->available();
    if (available > 0) {
      const size_t wanted = min(
        min(available, config_.downloadBufferBytes),
        manifest_.size - received
      );
      const int count = stream->readBytes(buffer.get(), wanted);
      if (count <= 0) break;
      if (Update.write(buffer.get(), count) != static_cast<size_t>(count)) {
        mbedtls_sha256_free(&sha);
        Update.abort();
        http.end();
        return fail(OtaResult::InstallFailed, String("flash write: ") + Update.errorString());
      }
      mbedtls_sha256_update_ret(&sha, buffer.get(), count);
      received += count;
      lastProgressAt = millis();
      service();
      continue;
    }

    if (!http.connected() || millis() - lastProgressAt > config_.requestTimeoutMs) break;
    service();
    delay(1);
  }

  uint8_t digest[32];
  mbedtls_sha256_finish_ret(&sha, digest);
  mbedtls_sha256_free(&sha);
  http.end();

  if (received != manifest_.size) {
    Update.abort();
    return fail(OtaResult::DownloadFailed, "firmware download ended early");
  }
  if (digestHex(digest) != manifest_.sha256) {
    Update.abort();
    return fail(OtaResult::VerificationFailed, "firmware SHA-256 mismatch");
  }
  if (!Update.end()) {
    return fail(OtaResult::InstallFailed, String("Update.end: ") + Update.errorString());
  }

  status("installed", String("version ") + manifest_.version);
  return OtaResult::Installed;
}

OtaResult OtaUpdater::checkAndUpdate() {
  lastError_ = "";
  manifest_ = OtaManifest();
  if (!validConfiguration()) {
    return fail(OtaResult::InvalidConfiguration, lastError_);
  }

  status("checking", String("current version ") + config_.currentVersion);
  if (!fetchManifest()) {
    const bool malformed = lastError_.startsWith("manifest JSON:") ||
      lastError_.startsWith("manifest is ") ||
      lastError_.startsWith("manifest SHA-256");
    return fail(
      malformed ? OtaResult::InvalidManifest : OtaResult::ManifestRequestFailed,
      lastError_
    );
  }
  if (manifest_.target != config_.target) {
    return fail(
      OtaResult::TargetMismatch,
      String("manifest target ") + manifest_.target + " does not match " + config_.target
    );
  }
  if (manifest_.version <= config_.currentVersion) {
    status("up-to-date", String("version ") + config_.currentVersion);
    return OtaResult::UpToDate;
  }

  return downloadAndInstall();
}

bool OtaUpdater::pendingVerification() {
  const esp_partition_t* running = esp_ota_get_running_partition();
  if (!running) return false;
  esp_ota_img_states_t state;
  return esp_ota_get_state_partition(running, &state) == ESP_OK &&
    state == ESP_OTA_IMG_PENDING_VERIFY;
}

bool OtaUpdater::markBootOk() {
  return esp_ota_mark_app_valid_cancel_rollback() == ESP_OK;
}

void OtaUpdater::reboot() {
  delay(100);
  ESP.restart();
}

const char* OtaUpdater::resultName(OtaResult result) {
  switch (result) {
    case OtaResult::UpToDate: return "up-to-date";
    case OtaResult::Installed: return "installed";
    case OtaResult::InvalidConfiguration: return "invalid-configuration";
    case OtaResult::ManifestRequestFailed: return "manifest-request-failed";
    case OtaResult::InvalidManifest: return "invalid-manifest";
    case OtaResult::TargetMismatch: return "target-mismatch";
    case OtaResult::DownloadFailed: return "download-failed";
    case OtaResult::VerificationFailed: return "verification-failed";
    case OtaResult::InstallFailed: return "install-failed";
  }
  return "unknown";
}

}  // namespace holdfast
