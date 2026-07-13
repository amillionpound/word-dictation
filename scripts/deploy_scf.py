#!/usr/bin/env python3
"""
Deploy to Tencent Cloud SCF (Serverless Cloud Function).
Called by GitHub Actions workflow.

Required env vars:
  TENCENT_SECRET_ID   - Tencent Cloud API SecretId (from GitHub Secrets)
  TENCENT_SECRET_KEY  - Tencent Cloud API SecretKey (from GitHub Secrets)

Configurable (edit below if function name/namespace changes):
  SCF_REGION       - Region, e.g. ap-guangzhou
  SCF_FUNCTION_NAME - Function name in SCF console
  SCF_NAMESPACE    - Namespace (default is "default")
"""

import base64
import os
import sys
import json

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.scf.v20180416 import scf_client
from tencentcloud.scf.v20180416 import models as scf_models

# ==================== Configuration ====================
SCF_REGION = os.environ.get("SCF_REGION", "ap-guangzhou")
SCF_FUNCTION_NAME = os.environ.get("SCF_FUNCTION_NAME", "ca9zcay6yh")
SCF_NAMESPACE = os.environ.get("SCF_NAMESPACE", "default")
HANDLER = "app.main_handler"
RUNTIME = "Python3.9"


def main():
    secret_id = os.environ.get("TENCENT_SECRET_ID")
    secret_key = os.environ.get("TENCENT_SECRET_KEY")

    if not secret_id or not secret_key:
        print("ERROR: TENCENT_SECRET_ID and TENCENT_SECRET_KEY must be set")
        sys.exit(1)

    # Read deployment zip
    zip_path = "deploy.zip"
    if not os.path.exists(zip_path):
        print(f"ERROR: {zip_path} not found")
        sys.exit(1)

    with open(zip_path, "rb") as f:
        zip_b64 = base64.b64encode(f.read()).decode("utf-8")

    print(f"Deploy package size: {os.path.getsize(zip_path)} bytes")
    print(f"Function: {SCF_FUNCTION_NAME}")
    print(f"Namespace: {SCF_NAMESPACE}")
    print(f"Region: {SCF_REGION}")

    try:
        cred = credential.Credential(secret_id, secret_key)
        client = scf_client.ScfClient(cred, SCF_REGION)

        # Update function code
        req = scf_models.UpdateFunctionCodeRequest()
        req.FunctionName = SCF_FUNCTION_NAME
        req.Namespace = SCF_NAMESPACE
        req.ZipFile = zip_b64
        req.Handler = HANDLER

        resp = client.UpdateFunctionCode(req)
        print(f"SUCCESS: {resp.to_json_string()}")

        # Also publish a new version for stability
        try:
            ver_req = scf_models.PublishVersionRequest()
            ver_req.FunctionName = SCF_FUNCTION_NAME
            ver_req.Namespace = SCF_NAMESPACE
            ver_resp = client.PublishVersion(ver_req)
            print(f"Published version: {ver_resp.FunctionVersion}")
        except Exception as e:
            print(f"Version publish skipped: {e}")

    except TencentCloudSDKException as e:
        print(f"DEPLOY FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
