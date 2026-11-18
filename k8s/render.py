#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 k8s/deploy.env 填进 oc-timeliness.yaml 的占位符，生成可 apply 的清单。

为什么要有这一步：清单里唯一需要按环境改的就是「镜像地址」和「对外域名」，
把它们抽成 deploy.env 之后，升级镜像只改一行、换域名也只改一行，
不用手改 240 行 YAML（手改最容易漏 CronJob 里那份 image）。

用法：
    cp k8s/deploy.env.example k8s/deploy.env
    # 编辑 deploy.env（至少填 IMAGE 和 HOST）
    python k8s/render.py
    kubectl apply -f k8s/rendered/oc-timeliness.yaml
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "oc-timeliness.yaml"
ENV_FILE = BASE / "deploy.env"
OUT_DIR = BASE / "rendered"
OUT = OUT_DIR / "oc-timeliness.yaml"


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"缺少 {path.name}：先 cp deploy.env.example deploy.env 再填值")
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def normalize_host(raw: str) -> str:
    """允许直接粘 https://oc.corp.local/ 这种，统一成裸域名。"""
    h = raw.strip().lower()
    for p in ("https://", "http://"):
        if h.startswith(p):
            h = h[len(p):]
    return h.strip("/")


HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def main() -> int:
    env = load_env(ENV_FILE)
    image = env.get("IMAGE", "").strip()
    host = normalize_host(env.get("HOST", ""))

    problem = False
    if not image or "__" in image:
        print("✗ IMAGE 没填（harbor 里的完整镜像地址，含 tag）")
        problem = True
    elif "example.com" in image:
        print("! IMAGE 还是示例值 harbor.example.com，记得换成真实仓库")
    if not host or "__" in host:
        print("✗ HOST 没填 —— 这个值就是以后的固定访问地址")
        problem = True
    elif not HOST_RE.match(host):
        print(f"✗ HOST 格式不对：{host}（只填域名，如 oc.corp.local，不要带 https:// 或路径）")
        problem = True
    elif "example.com" in host:
        print("✗ HOST 还是示例域名 oc-timeliness.example.com，换成公司真实域名")
        problem = True
    if problem:
        return 2

    ingress_class = env.get("INGRESS_CLASS", "") or "nginx"
    storage_class = env.get("STORAGE_CLASS", "")
    tls_secret = env.get("TLS_SECRET", "")
    issuer = env.get("CLUSTER_ISSUER", "")

    text = SRC.read_text(encoding="utf-8")

    # 1) 空值删行：空字符串在 K8S 里不等于"走默认"，必须整行去掉
    text = re.sub(
        r'^  storageClassName: "__STORAGE_CLASS__"\n',
        f'  storageClassName: "{storage_class}"\n' if storage_class else "",
        text, flags=re.M,
    )
    text = text.replace(
        "    #__CERT_ISSUER_ANNOTATION__\n",
        f"    cert-manager.io/cluster-issuer: {issuer}\n" if issuer else "",
    )
    text = text.replace(
        "  #__TLS_BLOCK__\n",
        f'  tls:\n    - hosts: ["{host}"]\n      secretName: {tls_secret}\n' if tls_secret else "",
    )

    # 2) 普通占位符
    text = (text
            .replace("__IMAGE__", image)
            .replace("__INGRESS_CLASS__", ingress_class)
            .replace("__HOST__", host))

    left = [m for m in ("__IMAGE__", "__HOST__", "__INGRESS_CLASS__",
                        "__STORAGE_CLASS__", "__TLS_BLOCK__", "__CERT_ISSUER_ANNOTATION__")
            if m in text]
    if left:
        print("✗ 还有占位符没替换：", ", ".join(left))
        return 3

    # 3) 兜底校验：能解析、七类资源齐全、两份镜像一致
    try:
        import yaml  # noqa: WPS433
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            docs = [d for d in yaml.safe_load_all(text) if d]
        except Exception as exc:
            print(f"✗ 生成的 YAML 解析失败：{exc}")
            return 4
        kinds = {d.get("kind") for d in docs}
        want = {"Namespace", "ConfigMap", "PersistentVolumeClaim",
                "Deployment", "Service", "CronJob", "Ingress"}
        missing = want - kinds
        if missing:
            print("✗ 清单不完整，缺少：", ", ".join(sorted(missing)))
            return 4
        images: set[str] = set()
        for d in docs:
            spec = d.get("spec") or {}
            podspecs = [(spec.get("template") or {}).get("spec")]
            job = spec.get("jobTemplate") or {}
            podspecs.append(((job.get("spec") or {}).get("template") or {}).get("spec"))
            for ps in podspecs:
                for c in (ps or {}).get("containers") or []:
                    images.add(c.get("image"))
        if images != {image}:
            print("! Deployment / CronJob 的镜像与配置不一致：", images)
            return 4

    OUT_DIR.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8")

    scheme = "https" if tls_secret else "http"
    print(f"✓ 已生成 {OUT.relative_to(BASE.parent)}")
    print(f"  镜像    {image}")
    print(f"  固定地址 {scheme}://{host}")
    print(f"  Ingress {ingress_class}" + (f" / TLS secret {tls_secret}" if tls_secret else " / 无 TLS"))
    if storage_class:
        print(f"  存储类  {storage_class}")
    print("\n下一步：")
    print("  kubectl apply -f k8s/secret.yaml")
    print("  kubectl apply -f k8s/rendered/oc-timeliness.yaml")
    print("  kubectl -n oc-timeliness rollout status deploy/oc-timeliness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
