variable "BUH_CACHE_SCOPE" {
  default = "main"
}

variable "BUH_CACHE_LANE" {
  default = "local"
}

variable "BUH_TEST_IMAGE_TAG" {
  default = "ci"
}

group "default" {
  targets = ["testauth", "baseline", "browser"]
}

target "baseline" {
  context = "../.."
  dockerfile = "platform/testenv/Dockerfile.baseline"
  tags = ["buh/source-baseline:${BUH_TEST_IMAGE_TAG}"]
  cache-from = [
    "type=gha,scope=buh-baseline-main",
    "type=gha,scope=buh-baseline-${BUH_CACHE_SCOPE}",
  ]
  cache-to = ["type=gha,scope=buh-baseline-${BUH_CACHE_SCOPE},mode=max,timeout=10m,ignore-error=true"]
  output = ["type=docker"]
}

target "browser" {
  context = "../.."
  dockerfile = "platform/testenv/Dockerfile.playwright"
  tags = ["buh/source-playwright:${BUH_TEST_IMAGE_TAG}"]
  cache-from = [
    "type=gha,scope=buh-browser-main-${BUH_CACHE_LANE}",
    "type=gha,scope=buh-browser-${BUH_CACHE_SCOPE}-${BUH_CACHE_LANE}",
  ]
  cache-to = ["type=gha,scope=buh-browser-${BUH_CACHE_SCOPE}-${BUH_CACHE_LANE},mode=max,timeout=10m,ignore-error=true"]
  output = ["type=docker"]
}

target "testauth" {
  context = "../.."
  dockerfile = "platform/testenv/Dockerfile"
  tags = ["buh/source-testauth:${BUH_TEST_IMAGE_TAG}"]
  cache-from = [
    "type=gha,scope=buh-testauth-main-${BUH_CACHE_LANE}",
    "type=gha,scope=buh-testauth-${BUH_CACHE_SCOPE}-${BUH_CACHE_LANE}",
  ]
  cache-to = ["type=gha,scope=buh-testauth-${BUH_CACHE_SCOPE}-${BUH_CACHE_LANE},mode=max,timeout=10m,ignore-error=true"]
  output = ["type=docker"]
}
