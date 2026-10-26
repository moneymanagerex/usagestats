cd /home/public_html/usagestats

# 1) 写 requirements.txt 和 main.py（用上面的内容）
#    确保 main.py 里没有遗留的 API/ 引用

# 2) 建 venv + 装依赖
python3 -m venv venv
venv/bin/pip install -U pip
venv/bin/pip install -r requirements.txt

# 3) 权限
chown -R www-data:www-data venv
chmod 644 main.py requirements.txt

# 4) 起服务
systemctl daemon-reload
systemctl enable --now usagestats
systemctl status usagestats --no-pager
journalctl -u usagestats -n 20 --no-pager

# 5) 本地直连测试（先不经过 Nginx）
curl -s http://127.0.0.1:8000/healthz
# {"ok":true}
