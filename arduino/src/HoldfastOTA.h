#pragma once

#include <Arduino.h>

#include <functional>

class HTTPClient;

namespace holdfast {

enum class OtaResult {
  UpToDate,
  Installed,
  InvalidConfiguration,
  ManifestRequestFailed,
  InvalidManifest,
  TargetMismatch,
  DownloadFailed,
  VerificationFailed,
  InstallFailed,
};

struct OtaManifest {
  uint32_t version = 0;
  String target;
  String file;
  size_t size = 0;
  String sha256;
};

struct OtaConfig {
  const char* baseUrl = nullptr;
  const char* target = nullptr;
  const char* authToken = nullptr;
  const char* rootCa = nullptr;
  uint32_t currentVersion = 0;
  uint32_t requestTimeoutMs = 15000;
  size_t downloadBufferBytes = 4096;
};

class OtaUpdater {
 public:
  using StatusCallback = std::function<void(const char* phase, const char* message)>;
  using ServiceCallback = std::function<void()>;

  explicit OtaUpdater(const OtaConfig& config);

  void onStatus(StatusCallback callback);
  void onService(ServiceCallback callback);

  OtaResult checkAndUpdate();
  const OtaManifest& manifest() const;
  const String& lastError() const;

  static bool pendingVerification();
  static bool markBootOk();
  static void reboot();
  static const char* resultName(OtaResult result);

 private:
  bool validConfiguration();
  bool fetchManifest();
  OtaResult downloadAndInstall();
  void applyAuthorization(HTTPClient& http);
  void status(const char* phase, const String& message = String());
  void service();
  OtaResult fail(OtaResult result, const String& message);

  OtaConfig config_;
  OtaManifest manifest_;
  StatusCallback statusCallback_;
  ServiceCallback serviceCallback_;
  String lastError_;
};

}  // namespace holdfast
