ARG AA_DOCKER_TAG
FROM $AA_DOCKER_TAG

WORKDIR ${AUTH_HOME}

COPY /conf/requirements.txt requirements.txt
RUN --mount=type=cache,target=~/.cache \
    pip install -r requirements.txt

COPY /conf/aa_buh_memberaudit_autoreg-0.1.0-py3-none-any.whl /tmp/
RUN pip install /tmp/aa_buh_memberaudit_autoreg-0.1.0-py3-none-any.whl

# B-UH Mining Analytics
COPY conf/aa_buh_mining_analytics-0.1.1-py3-none-any.whl /tmp/
RUN pip install /tmp/aa_buh_mining_analytics-0.1.1-py3-none-any.whl


# BEGIN B-UH STRUCTURE OPERATIONS
COPY conf/aa_structures-4.0.3-py3-none-any.whl /tmp/
COPY conf/aa_moonmining-3.1.0.post1-py3-none-any.whl /tmp/
COPY conf/aa_buh_structure_ops-0.2.1-py3-none-any.whl /tmp/
RUN python3 -m pip install --no-cache-dir \
    /tmp/aa_structures-4.0.3-py3-none-any.whl \
    /tmp/aa_moonmining-3.1.0.post1-py3-none-any.whl \
    /tmp/aa_buh_structure_ops-0.2.1-py3-none-any.whl
# END B-UH STRUCTURE OPERATIONS


# BEGIN B-UH VPS HEALTH
COPY conf/aa_buh_vps_health-0.2.0-py3-none-any.whl /tmp/
RUN python3 -m pip install --no-cache-dir --no-deps /tmp/aa_buh_vps_health-0.2.0-py3-none-any.whl
# END B-UH VPS HEALTH

# BEGIN B-UH MAX HISTORY
COPY conf/aa_buh_max_history-2.0.0-py3-none-any.whl /tmp/
RUN python3 -m pip install --no-cache-dir --no-deps /tmp/aa_buh_max_history-2.0.0-py3-none-any.whl
# END B-UH MAX HISTORY

