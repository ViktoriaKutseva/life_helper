FROM python:3.12-slim

WORKDIR /app

COPY /app/ .

ENV PYTHONPATH=/app:$PYTHONPATH
ENV PYTHONPATH=/:$PYTHONPATH

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run the bot
CMD ["python", "main.py"] 