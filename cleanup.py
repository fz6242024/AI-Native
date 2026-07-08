with open(".github/workflows/alphasift_main.yml", "r", encoding="utf-8") as f:
    content = f.read()

# 去掉调试输出，恢复简洁版本
content = content.replace(
    '''          echo "=== Payload B: $PAYLOAD_B ==="
          CURL_RESPONSE_B=$(curl -s -w "\\n%{http_code}" \\
            -X POST \\
            -H "Accept: application/vnd.github.v3+json" \\
            -H "Authorization: token $GH_PAT_ACCOUNT_B" \\
            -d "$PAYLOAD_B" \\
            "https://api.github.com/repos/zf6242206/AI-Native/actions/workflows/skills_runner.yml/dispatches")
          HTTP_CODE_B=$(echo "$CURL_RESPONSE_B" | tail -1)
          echo "=== Response B: $CURL_RESPONSE_B ==="''',
    '''          HTTP_CODE_B=$(curl -s -o /dev/null -w "%{http_code}" \\
            -X POST \\
            -H "Accept: application/vnd.github.v3+json" \\
            -H "Authorization: token $GH_PAT_ACCOUNT_B" \\
            -d "$PAYLOAD_B" \\
            "https://api.github.com/repos/zf6242206/AI-Native/actions/workflows/skills_runner.yml/dispatches" || echo "000")'''
)

content = content.replace(
    '''          echo "=== Payload C: $PAYLOAD_C ==="
          CURL_RESPONSE_C=$(curl -s -w "\\n%{http_code}" \\
            -X POST \\
            -H "Accept: application/vnd.github.v3+json" \\
            -H "Authorization: token $GH_PAT_ACCOUNT_C" \\
            -d "$PAYLOAD_C" \\
            "https://api.github.com/repos/fz6242025/AI-Native/actions/workflows/skills_runner.yml/dispatches")
          HTTP_CODE_C=$(echo "$CURL_RESPONSE_C" | tail -1)
          echo "=== Response C: $CURL_RESPONSE_C ==="''',
    '''          HTTP_CODE_C=$(curl -s -o /dev/null -w "%{http_code}" \\
            -X POST \\
            -H "Accept: application/vnd.github.v3+json" \\
            -H "Authorization: token $GH_PAT_ACCOUNT_C" \\
            -d "$PAYLOAD_C" \\
            "https://api.github.com/repos/fz6242025/AI-Native/actions/workflows/skills_runner.yml/dispatches" || echo "000")'''
)

with open(".github/workflows/alphasift_main.yml", "w", encoding="utf-8") as f:
    f.write(content)
print("Cleaned up debug output")
