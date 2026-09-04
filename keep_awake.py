import re
import sys
import time
from playwright.sync_api import sync_playwright

APP_URL = "https://consmanager.streamlit.app/"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print(f"방문: {APP_URL}")
        page.goto(APP_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # 잠자기 화면인지 확인 후, 있으면 깨우기 버튼 클릭
        wake_button = page.get_by_text(re.compile("get this app back up", re.IGNORECASE))
        try:
            if wake_button.is_visible(timeout=5000):
                print("잠들어 있음 -> 깨우기 버튼 클릭")
                wake_button.click()
                # 앱이 다시 켜질 때까지 최대 3분 대기
                page.wait_for_selector("div[data-testid='stAppViewContainer']", timeout=180000)
                print("깨움 완료, 앱 로드 확인됨")
            else:
                print("이미 깨어있음")
        except Exception:
            print("잠자기 버튼 없음 -> 이미 정상 작동 중으로 판단")

        # 방문이 제대로 기록되도록 잠깐 대기
        page.wait_for_timeout(3000)
        browser.close()
        print("완료")


if __name__ == "__main__":
    main()