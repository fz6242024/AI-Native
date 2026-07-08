with open(".github/workflows/alphasift_main.yml", "r", encoding="utf-8") as f:
    content = f.read()

# 在 curl 命令后面加一步：打印完整的 HTTP 响应（不只是状态码）
old_b = '''          PAYLOAD_B=$(jq -n --arg stocks "$FIRST5_JSON" '"'"'{"inputs":{"stocks":$stocks}}'"'"')
          HTTP_CODE_B=$(curl -s -o /dev/null -w "%{http_code}" \\
            -X POST \\
            -H "Accept: application/vnd.github.v3+json" \\
            -H "Authorization: token $GH_PAT_ACCOUNT_B" \\
            -d "$PAYLOAD_B" \\
            "https://api.github.com/repos/zf6242206/AI-Native/actions/workflows/skills_runner.yml/dispatches" || echo "000")'''

new_b = '''          PAYLOAD_B=$(jq -n --arg stocks "$FIRST5_JSON" '"'"'{"inputs":{"stocks":$stocks}}'"'"')
          echo "=== Payload B: $PAYLOAD_B ==="
          CURL_RESPONSE_B=$(curl -s -w "\\n%{http_code}" \\
            -X POST \\
            -H "Accept: application/vnd.github.v3+json" \\
            -H "Authorization: token $GH_PAT_ACCOUNT_B" \\
            -d "$PAYLOAD_B" \\
            "https://api.github.com/repos/zf6242206/AI-Native/actions/workflows/skills_runner.yml/dispatches")
          HTTP_CODE_B=$(echo "$CURL_RESPONSE_B" | tail -1)
          echo "=== Response B: $CURL_RESPONSE_B ==="'''

content = content.replace(old_b, new_b)

old_c = '''          PAYLOAD_C=$(jq -n --arg stocks "$LAST5_JSON" '"'"'{"inputs":{"stocks":$stocks}}'"'"')
          HTTP_CODE_C=$(curl -s -o /dev/null -w "%{http_code}" \\
            -X POST \\
            -H "Accept: application/vnd.github.v3+json" \\
            -H "Authorization: token $GH_PAT_ACCOUNT_C" \\
            -d "$PAYLOAD_C" \\
            "https://api.github.com/repos/fz6242025/AI-Native/actions/workflows/skills_runner.yml/dispatches" || echo "000")'''

new_c = '''          PAYLOAD_C=$(jq -n --arg stocks "$LAST5_JSON" '"'"'{"inputs":{"stocks":$stocks}}'"'"')
          echo "=== Payload C: $PAYLOAD_C ==="
          CURL_RESPONSE_C=$(curl -s -w "\\n%{http_code}" \\
            -X POST \\
            -H "Accept: application/vnd.github.v3+json" \\
            -H "Authorization: token $GH_PAT_ACCOUNT_C" \\
            -d "$PAYLOAD_C" \\
            "https://api.github.com/repos/fz6242025/AI-Native/actions/workflows/skills_runner.yml/dispatches")
          HTTP_CODE_C=$(echo "$CURL_RESPONSE_C" | tail -1)
          echo "=== Response C: $CURL_RESPONSE_C ==="'''

content = content.replace(old_c, new_c)

with open(".github/workflows/alphasift_main.yml", "w", encoding="utf-8") as f:
    f.write(content)
print("Added debug output to curl responses")
