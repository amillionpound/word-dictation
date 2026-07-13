#!/usr/bin/env python3
"""
Deploy to Tencent Cloud SCF (Serverless Cloud Function).
Called by GitHub Actions workflow.

Reused across projects — each repo has a .scf-deploy.json config:
  {
    "function_name": "word-dictation",
    "namespace": "default",
    "region": "ap-guangzhou",
    "handler": "app.main_handler",
    "files": ["app.py", "index.html"]
  }

Required env vars (from GitHub Secrets):
  TENCENT_SECRET_ID
  TENCENT_SECRET_KEY
"""

import base64
import json
import os
import sys
import zipfile

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.scf.v20180416 import scf_client
from tencentcloud.scf.v20180416 import models as scf_models


def load_config():
    """Load deploy config from .scf-deploy.json, fall back to env vars."""
    config_path = ".scf-deploy.json"
    cfg = {
        "function_name": os.environ.get("SCF_FUNCTION_NAME", ""),
        "namespace": "default",
        "region": "ap-guangzhou",
        "handler": "app.main_handler",
        "files": ["app.py", "index.html"],
    }
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            file_cfg = json.load(f)
        cfg.update(file_cfg)

    if not cfg["function_name"]:
        print("ERROR: function_name not set. Create .scf-deploy.json or set SCF_FUNCTION_NAME env var.")
        sys.exit(1)

    return cfg


def main():
    secret_id = os.environ.get("TENCENT_SECRET_ID")
    secret_key = os.environ.get("TENCENT_SECRET_KEY")

    if not secret_id or not secret_key:
        print("ERROR: TENCENT_SECRET_ID and TENCENT_SECRET_KEY must be set")
        sys.exit(1)

    cfg = load_config()

    # Create deployment zip
    zip_path = "deploy.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in cfg["files"]:
            if os.path.exists(f):
                z.write(f)
                print(f"  Added: {f}")
            else:
                print(f"  WARNING: {f} not found, skipping")

    with open(zip_path, "rb") as f:
        zip_b64 = base64.b64encode(f.read()).decode("utf-8")

    print(f"Deploy package: {os.path.getsize(zip_path)} bytes")
    print(f"Function: {cfg['function_name']} (ns={cfg['namespace']}, region={cfg['region']})")

    try:
        cred = credential.Credential(secret_id, secret_key)
        client = scf_client.ScfClient(cred, cfg["region"])

        # Update function code
        req = scf_models.UpdateFunctionCodeRequest()
        req.FunctionName = cfg["function_name"]
        req.Namespace = cfg["namespace"]
        req.ZipFile = zip_b64
        req.Handler = cfg["handler"]

        resp = client.UpdateFunctionCode(req)
        print(f"DEPLOY SUCCESS: RequestId={resp.RequestId}")

        # Publish a new version for stability
        try:
            ver_req = scf_models.PublishVersionRequest()
            ver_req.FunctionName = cfg["function_name"]
            ver_req.Namespace = cfg["namespace"]
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
