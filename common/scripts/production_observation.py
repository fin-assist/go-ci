"""Wait for cluster decisions authenticated by the private receipt writer."""
import time

from production_rollback import RollbackError, receipts, trusted_payloads, validate_image


def event(service, version, operation, creator, task, get, *, image=None):
    match = None
    for entry in trusted_payloads(service, creator, task, get):
        if entry["version"] != version:
            continue
        if entry.get("operation") != operation:
            raise RollbackError("Cluster receipt operation differs from the command")
        common = {"schema", "service", "operation", "version"}
        if task == "observe-production-release":
            if set(entry) != common | {"stable_image", "candidate"} or type(entry["candidate"]) is not bool:
                raise RollbackError("Invalid cluster observation")
            if entry["stable_image"] is not None:
                validate_image(service, entry["stable_image"])
        elif task == "reject-production-observation":
            if set(entry) != common | {"status"} or entry["status"] != "failure":
                raise RollbackError("Invalid observation failure")
        else:
            if (set(entry) != common | {"image", "status"} or entry["image"] != image
                    or entry["status"] not in ("success", "failure")
                    or task == "reject-production-release" and entry["status"] != "failure"):
                raise RollbackError("Invalid cluster outcome")
        if match is not None and match != entry:
            raise RollbackError("Conflicting cluster signals for the same command")
        match = entry
    return match


def rollback_retry_complete(service, head, live, creator, get):
    """An accepted partial rollback may recover only its original target."""
    def signal(task):
        return event(service, head.version, "rollback", creator, task, get, image=head.image)

    if signal("reject-production-release"):
        raise RollbackError("The previous attempt was rejected; start a new Rollback production run")
    finished = signal("finish-production-rollback")
    if finished and finished["status"] == "success":
        if live["stable_image"] == head.image and not live["candidate"]:
            return True
        raise RollbackError("The completed rollback has been superseded; start a new Rollback production run")

    prior = signal("observe-production-release")
    if prior and live["stable_image"] in (prior["stable_image"], head.image):
        return False
    if live["stable_image"] is None:
        accepted = receipts(service, creator, get).get(head.version)
        if accepted and accepted["image"] == head.image:
            return False  # Recover an accepted target with temporarily unready endpoints.
    raise RollbackError("The stable image changed after the previous attempt; start a new Rollback production run")


def wait(service, version, operation, creator, get, *, image=None):
    for poll in range(61):
        if image is None:
            if event(service, version, operation, creator, "reject-production-observation", get):
                raise RollbackError("Cluster could not establish a stable observation for this command")
            observed = event(service, version, operation, creator, "observe-production-release", get)
            if observed is not None:
                return observed
        else:
            rejected = event(service, version, operation, creator, "reject-production-release", get, image=image)
            if rejected:
                raise RollbackError("Cluster rejected this release after observing the active service")
            if operation == "rollback":
                result = event(service, version, operation, creator, "finish-production-rollback", get, image=image)
                if result:
                    if result["status"] == "failure":
                        raise RollbackError("Cluster could not complete this rollback")
                    return result
            else:
                accepted = receipts(service, creator, get).get(version)
                if accepted:
                    if accepted["image"] != image:
                        raise RollbackError("Accepted service image differs from the requested image")
                    return accepted
        if poll < 60:
            time.sleep(10)
    raise RollbackError("Timed out waiting for the cluster receipt; inspect the service Application in Argo CD")
