# Casa Browser's integration test image: the app image, unchanged, plus the pinned test
# dependencies and the test tree. run-integration.sh starts it with the image's own s6 /init,
# so the production oneshots and services run exactly as under Home Assistant; the tests then
# probe each service user from inside the container. Nothing here is shipped.
ARG APP_IMAGE=casa-browser:it-app
FROM ${APP_IMAGE}
COPY requirements-test.txt /tmp/casa_browser-requirements-test.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/casa_browser-requirements-test.txt \
    && rm /tmp/casa_browser-requirements-test.txt
COPY pytest.ini /opt/casa_browser_tests/pytest.ini
COPY tests /opt/casa_browser_tests/tests
