"""M4.2 BUILDER: minimal Task API (POST/GET /api/tasks, GET /api/tasks/{id}).

Scope: HTTP layer + TaskEvent bookkeeping only, using the pure
`app.orchestration.state_machine.dependencies_satisfied` guard. No Claude
Code invocation, no subprocess, no orchestrator loop -- that is
INTELLIGENCE's scope, exercised in test_orchestration_state_machine.py /
test_m4_1_reviewer_orchestration.py instead.
"""

MINIMAL_TASK = {
    "goal": "Write the M4.2 task API",
    "role": "builder",
}


def create_task(client, **overrides):
    payload = {**MINIMAL_TASK, **overrides}
    return client.post("/api/tasks", json=payload)


def test_create_task_without_dependencies_goes_straight_to_ready(client):
    response = create_task(client)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "READY"
    assert body["goal"] == MINIMAL_TASK["goal"]
    assert body["role"] == MINIMAL_TASK["role"]
    assert body["depends_on"] == []
    assert body["attempt_count"] == 0
    assert body["max_attempts"] == 2

    detail = client.get(f"/api/tasks/{body['id']}").json()
    assert detail["status"] == "READY"


def test_create_task_with_all_dependencies_done_goes_to_ready(client):
    dep = create_task(client, goal="dependency task").json()

    # No PATCH endpoint exists (out of M4.2 scope), so reach through the
    # app's own dependency-injected DB session -- the client fixture
    # overrides get_db with an isolated in-memory engine -- to simulate a
    # finished dependency.
    from app.main import app
    from app.db.session import get_db as get_db_dep
    from app.models.entities import Task

    db_gen = app.dependency_overrides[get_db_dep]()
    db = next(db_gen)
    try:
        task = db.get(Task, dep["id"])
        task.status = "DONE"
        db.commit()
    finally:
        db_gen.close()

    child = create_task(client, goal="child task", depends_on=[dep["id"]])
    assert child.status_code == 201
    assert child.json()["status"] == "READY"
    assert child.json()["depends_on"] == [dep["id"]]


def test_create_task_with_pending_dependency_stays_planned(client):
    dep = create_task(client, goal="pending dependency").json()
    assert dep["status"] == "READY"  # dep itself has no deps, so it's READY, not DONE

    child = create_task(client, goal="blocked child", depends_on=[dep["id"]])
    assert child.status_code == 201
    assert child.json()["status"] == "PLANNED"


def test_create_task_with_unknown_dependency_stays_planned(client):
    response = create_task(client, depends_on=[999999])
    assert response.status_code == 201
    assert response.json()["status"] == "PLANNED"


def test_create_task_writes_task_events(client):
    from app.main import app
    from app.db.session import get_db as get_db_dep
    from app.models.entities import TaskEvent

    created = create_task(client).json()

    db_gen = app.dependency_overrides[get_db_dep]()
    db = next(db_gen)
    try:
        events = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id == created["id"])
            .order_by(TaskEvent.id)
            .all()
        )
    finally:
        db_gen.close()

    assert [(e.from_state, e.to_state, e.actor) for e in events] == [
        (None, "PLANNED", "human"),
        ("PLANNED", "READY", "orchestrator"),
    ]


def test_create_task_without_ready_dependency_writes_only_planned_event(client):
    from app.main import app
    from app.db.session import get_db as get_db_dep
    from app.models.entities import TaskEvent

    dep = create_task(client, goal="pending dep").json()
    child = create_task(client, goal="child", depends_on=[dep["id"]]).json()

    db_gen = app.dependency_overrides[get_db_dep]()
    db = next(db_gen)
    try:
        events = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id == child["id"])
            .order_by(TaskEvent.id)
            .all()
        )
    finally:
        db_gen.close()

    assert [(e.from_state, e.to_state, e.actor) for e in events] == [
        (None, "PLANNED", "human"),
    ]


def test_list_tasks_returns_summary_fields(client):
    create_task(client, goal="first task")
    create_task(client, goal="second task")

    listed = client.get("/api/tasks").json()
    assert len(listed) == 2
    for item in listed:
        assert set(item.keys()) == {"id", "goal", "role", "status", "created_at"}


def test_get_task_returns_full_detail_and_attempt_count(client):
    created = create_task(
        client,
        instructions="do the thing",
        allowed_resources=["backend/app/api/routes.py"],
        forbidden_actions=["subprocess"],
        acceptance_criteria="tests pass",
        timeout_seconds=3600,
        max_attempts=3,
        budget_eur=5.0,
        provider="anthropic",
        model="claude-sonnet-5",
    ).json()

    detail = client.get(f"/api/tasks/{created['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["instructions"] == "do the thing"
    assert body["allowed_resources"] == ["backend/app/api/routes.py"]
    assert body["forbidden_actions"] == ["subprocess"]
    assert body["acceptance_criteria"] == "tests pass"
    assert body["timeout_seconds"] == 3600
    assert body["max_attempts"] == 3
    assert body["budget_eur"] == 5.0
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-sonnet-5"
    assert body["attempt_count"] == 0
    assert body["required_approvals"] == []


def test_get_unknown_task_returns_404(client):
    response = client.get("/api/tasks/999999")
    assert response.status_code == 404


def test_create_task_rejects_empty_goal(client):
    response = create_task(client, goal="")
    assert response.status_code == 422


def test_create_task_rejects_missing_role(client):
    response = client.post("/api/tasks", json={"goal": "no role given"})
    assert response.status_code == 422


def test_create_task_rejects_empty_role(client):
    response = create_task(client, role="")
    assert response.status_code == 422
