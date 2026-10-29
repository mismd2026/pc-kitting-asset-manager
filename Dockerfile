FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# The worker runs Ansible against Windows (WinRM); the API image shares the same build.
RUN pip install ".[postgres,ansible]" \
 && ansible-galaxy collection install ansible.windows community.windows microsoft.ad chocolatey.chocolatey
COPY ansible ./ansible
COPY imaging ./imaging

RUN useradd --create-home --uid 10001 app && cp -r /root/.ansible /home/app/ && chown -R app /home/app/.ansible
USER app
ENV ANSIBLE_DIR=/app/ansible ANSIBLE_CONFIG=/app/ansible/ansible.cfg
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["uvicorn", "assetdb.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
