# Lecture Slide Capture

강의 영상이나 브라우저 창을 지켜보다가 슬라이드가 바뀌는 순간만 로컬에 저장하고, 캡처된 슬라이드를 이미지와 `slides.pdf`로 정리하는 데스크톱 앱입니다.

![Lecture Slide Capture app screenshot](design/lecture-slide-capture-app-screenshot.png)

## 왜 만들었나요

강의 녹화는 길고 반복적인 경우가 많지만, 복습할 때 실제로 필요한 것은 전체 영상보다 슬라이드 상태인 경우가 많습니다. Lecture Slide Capture는 강의 창에서 슬라이드 전환을 감지해 의미 있는 프레임만 저장하고, 이를 복습하기 쉬운 PDF로 묶어 줍니다.

이 앱은 개인정보와 로컬 작업 흐름을 우선합니다.

- 사용자의 기기에서 실행됩니다.
- 강의 영상이나 스크린샷을 서버로 업로드하지 않습니다.
- 슬라이드 이미지, 로그, PDF는 로컬 세션 폴더에 저장됩니다.
- 브라우저 창 또는 사용자가 직접 지정한 화면 영역을 캡처할 수 있습니다.

## 이런 사용자에게 유용합니다

- 온라인 강의를 복습하며 슬라이드를 일일이 캡처하고 싶지 않은 학생.
- 녹화된 강의에서 빠르게 슬라이드 스냅샷을 얻고 싶은 강의자와 조교.
- 로컬 우선 노트 정리 및 강의 처리 워크플로를 만드는 연구자.
- 시각 자료를 PDF 형태로 압축해 보고 싶은 접근성 중심 사용자.

## 주요 기능

- GUI에서 Chrome 강의 창 또는 화면 영역 선택.
- 슬라이드 ROI 지정으로 브라우저 UI, 자막, 조작 버튼 제외.
- 슬라이드 전환 시점만 감지해 이미지 저장.
- 캡처 중 최근 저장 슬라이드와 저장 목록 확인.
- 세션 종료 시 `slides.pdf` 생성.
- 캡처 일시정지, 재개, 안전한 종료.
- 기본 저장 위치 기억.
- 앱 언어 설정에서 English/한국어 전환.
- 실행 가능한 macOS `.app` 번들과 Windows 빌드 파일 포함.

## 빠른 시작

### macOS

1. `Lecture Slide Capture.app`를 실행합니다.
2. Python 패키지가 없으면 앱이 안내하는 설치 명령을 실행합니다.
3. Chrome 강의 창 또는 화면 영역을 선택합니다.
4. 슬라이드 영역을 지정합니다.
5. `Start Capture`를 누릅니다.
6. 완료 후 `Finish`를 눌러 `slides.pdf`를 생성합니다.

macOS에서는 화면 기록 권한이 필요할 수 있습니다. 캡처 화면이 비어 있다면 시스템 설정에서 권한을 허용하고 앱을 다시 실행해 주세요.

### Windows

Windows PC에서 빌드하거나 GitHub Actions의 `Build Windows` workflow를 실행합니다. PyInstaller는 macOS에서 Windows 실행 파일을 교차 빌드하지 못합니다.

```powershell
.\scripts\build_windows.ps1
```

결과 실행 파일은 아래 경로에 생성됩니다.

```text
dist\LectureSlideCapture.exe
```

## 출력

각 실행은 선택한 기본 저장 위치 아래에 타임스탬프 세션 폴더를 만듭니다. 기본 저장 위치는 GUI에서 바꿀 수 있고 로컬에 저장됩니다.

일반적인 세션 구성:

```text
slide_0001.png
slide_0002.png
slides.pdf
captures.csv
duplicates.csv
capture_source.json
```

## 저장소 구성

```text
Lecture Slide Capture.app/
  Contents/Resources/slide_capture_gui.py   GUI 프론트엔드
  Contents/Resources/slide_capture.py       캡처 엔진
  Contents/Resources/requirements.txt       Python 의존성
design/
  lecture-slide-capture-app-screenshot.png  실제 앱 스크린샷
packaging/windows/
  LectureSlideCapture.windows.spec          PyInstaller spec
scripts/
  build_windows.ps1                         Windows 빌드 스크립트
```

## 검증

현재 앱 번들은 아래 명령으로 확인했습니다.

```sh
python3 -m py_compile "Lecture Slide Capture.app/Contents/Resources/slide_capture_gui.py" \
  "Lecture Slide Capture.app/Contents/Resources/slide_capture.py"
bash -n "Lecture Slide Capture.app/Contents/Resources/run_capture_in_terminal.sh"
```

실제 캡처 동작은 운영체제 화면 기록 권한과 현재 열려 있는 강의 창 상태에 영향을 받습니다.

## 로드맵

- Windows 창 캡처 안정성과 패키징 문서 개선.
- 슬라이드 전환 감지 회귀를 확인할 작은 샘플 fixture 추가.
- 화면 기록 권한 누락 시 더 명확한 첫 실행 진단 제공.
- Tkinter GUI의 키보드 조작과 접근성 개선.
- macOS와 Windows용 다운로드 가능한 릴리즈 artifact 게시.

## 기여하기

이슈와 Pull Request를 환영합니다. 개발 방법, 개인정보 보호 원칙, 시작하기 좋은 기여 항목은 [CONTRIBUTING.md](./CONTRIBUTING.md)를 참고해 주세요.

## 라이선스

MIT. 자세한 내용은 [LICENSE](./LICENSE)를 확인해 주세요.

## English

For English documentation, see [README.md](./README.md).
