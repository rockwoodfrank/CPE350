@REM Start the python environment
CALL C:\Users\P311248\AppData\Local\anaconda3\Scripts\activate
CALL conda activate base



@REM Client & Viewer
start cmd /k "C:\Users\P311248\AppData\Local\anaconda3\Scripts\activate.bat && conda activate base && cd graph && python client.py"

@REM AI Server
cd bosch-metadata-reader
Uvicorn server:app --reload