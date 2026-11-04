@echo off
REM Windows: 가상환경 생성 + 패키지 설치
cd /d "%~dp0"
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo 설치 완료. 다음 순서로 진행하세요:
echo    1) .env 파일에 API 키 입력
echo    2) .venv\Scripts\activate.bat
echo    3) python main.py --limit 1 --no-publish
