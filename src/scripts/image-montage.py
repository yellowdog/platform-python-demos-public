# ---
# jupyter:
#   kernelspec:
#     display_name: Python3
#     language: python
#     name: python3
#   language_info:
#     codemirror_mode:
#       name: ipython
#       version: 3
#     file_extension: ".py"
#     mimetype: "text/x-python"
#     name: "python"
#     nbconvert_exporter: "python"
#     pygments_lexer: "ipython3"
#     version: "3.8.5"
# ---

# %% [markdown]
# # Configuration

# %%
import os

from yellowdog_client import PlatformClient
from yellowdog_client.common.server_sent_events import DelegatedSubscriptionEventListener
from yellowdog_client.object_store.model import FileTransferStatus
from yellowdog_client.model import ServicesSchema, ApiKey, ComputeRequirementDynamicTemplate, \
    StringAttributeConstraint, WorkRequirement, TaskGroup, RunSpecification, Task, TaskInput, TaskOutput, FlattenPath, \
    ComputeRequirementTemplateUsage, ProvisionedWorkerPoolProperties, WorkRequirementStatus, TaskStatus, \
    TaskInputSource, TaskOutputSource, WorkerClaimBehaviour, MachineImageFamilySearch

from utils.common import generate_unique_name, markdown, link, link_entity, use_template, image, script_relative_path

key = os.environ['KEY']
secret = os.environ['SECRET']
url = os.environ['URL']
namespace = os.environ['NAMESPACE']
template_id = os.environ.get('TEMPLATE_ID')
auto_shutdown = os.environ['AUTO_SHUTDOWN'] == "True"

run_id = generate_unique_name(namespace)

client = PlatformClient.create(
    ServicesSchema(defaultUrl=url),
    ApiKey(key, secret)
)

image_family = "yd-agent-docker"

images = client.images_client.search_image_families(MachineImageFamilySearch(
    includePublic=True,
    namespace="YellowDog",
    familyName=image_family
))

images = [image for image in images if image.name == image_family]

if not images:
    raise Exception("Unable to find ID for image family: " + image_family)
elif len(images) > 1:
    raise Exception("Multiple matching image families found")

default_template = ComputeRequirementDynamicTemplate(
    name=run_id,
    strategyType='co.yellowdog.platform.model.SingleSourceProvisionStrategy',
    imagesId=images[0].id,
    constraints=[
        StringAttributeConstraint(attribute='source.provider', anyOf={'AWS'}),
        StringAttributeConstraint(attribute='source.instanceType', anyOf={"t3a.small"})
    ],
)

markdown("Configured to run against", link(url))

# %% [markdown]
# # Upload source picture to Object Store

# %%
source_picture_path = script_relative_path("resources/ImageMontage.jpg")
source_picture_file = os.path.basename(source_picture_path)
client.object_store_client.start_transfers()
session = client.object_store_client.create_upload_session(namespace, source_picture_path)
markdown("Waiting for source picture to upload to Object Store...")
session.start()
session = session.when_status_matches(lambda status: status.is_finished()).result()

if session.status != FileTransferStatus.Completed:
    raise Exception("Source picture failed to upload. Status: " + session)

stats = session.get_statistics()
markdown(link(
    url,
    f"#/objects/{namespace}/{source_picture_file}?object=true",
    f"Upload {session.status.name.lower()} ({stats.bytes_transferred}B uploaded)"
))

# %% [markdown]
# # Provision Worker Pool

# %%

with use_template(client, template_id, default_template) as template_id:
    worker_pool = client.worker_pool_client.provision_worker_pool(
        ComputeRequirementTemplateUsage(
            templateId=template_id,
            requirementNamespace=namespace,
            requirementName=run_id,
            targetInstanceCount=5
        ),
        ProvisionedWorkerPoolProperties(
            workerTag=run_id,
            autoShutdown=auto_shutdown
        )
    )

markdown("Added", link_entity(url, worker_pool))

# %% [markdown]
# # Add Work Requirement

# %%
image_processors_task_group_name = "ImageProcessors"
montage_task_group_name = "ImageMontage"

work_requirement = WorkRequirement(
    namespace=namespace,
    name=run_id,
    taskGroups=[
        TaskGroup(
            name=image_processors_task_group_name,
            runSpecification=RunSpecification(
                minimumQueueConcurrency=5,
                idealQueueConcurrency=5,
                workerClaimBehaviour=WorkerClaimBehaviour.MAINTAIN_IDEAL,
                taskTypes=["docker"],
                maximumTaskRetries=3,
                workerTags=[run_id],
                shareWorkers=True
            )
        ),
        TaskGroup(
            name=montage_task_group_name,
            runSpecification=RunSpecification(
                minimumQueueConcurrency=1,
                idealQueueConcurrency=1,
                workerClaimBehaviour=WorkerClaimBehaviour.MAINTAIN_IDEAL,
                taskTypes=["docker"],
                maximumTaskRetries=3,
                workerTags=[run_id],
                shareWorkers=True
            ),
            dependentOn=image_processors_task_group_name,
        )
    ]
)

work_requirement = client.work_client.add_work_requirement(work_requirement)
markdown("Added", link_entity(url, work_requirement))

# %% [markdown]
# # Add Tasks to Work Requirement

# %%


def generate_task(task_name: str, conversion: str, output_file: str) -> Task:
    return Task(
        name=task_name,
        taskType="docker",
        inputs=[TaskInput(TaskInputSource.TASK_NAMESPACE, source_picture_file)],
        taskData=f"v4tech/imagemagick convert {conversion} /yd_working/{source_picture_file} /yd_working/{output_file}",
        outputs=[
            TaskOutput(TaskOutputSource.WORKER_DIRECTORY, filePattern=output_file),
            TaskOutput(TaskOutputSource.PROCESS_OUTPUT, uploadOnFailed=True)
        ]
    )


montage_picture_file = "montage_" + source_picture_file

conversions = {
    "negate": "-negate",
    "paint": "-paint 10",
    "charcoal": "-charcoal 2",
    "pixelate": "-scale 2%% -scale 600x400",
    "vignette": "-background black -vignette 0x1",
    "blur": "-morphology Convolve Blur:0x25",
    "mask": "-fuzz 15%% -transparent white -alpha extract -negate"
}

client.work_client.add_tasks_to_task_group_by_name(
    namespace,
    work_requirement.name,
    image_processors_task_group_name,
    [generate_task(k + "_image", v, k + "_" + source_picture_file) for k, v in conversions.items()]
)

files = [source_picture_file]
files.extend([k + "_" + source_picture_file for k, v in conversions.items()])
files.append("montage_" + source_picture_file)
files = " ".join(["/yd_working/" + f for f in files])

montage_task_name = "MontageImage"
client.work_client.add_tasks_to_task_group_by_name(namespace, work_requirement.name, montage_task_group_name, [
    Task(
        name=montage_task_name,
        taskType="docker",
        inputs=[
            TaskInput(TaskInputSource.TASK_NAMESPACE, source_picture_file),
            TaskInput(TaskInputSource.TASK_NAMESPACE, f"{work_requirement.name}/**/*_{source_picture_file}")
        ],
        flattenInputPaths=FlattenPath.FILE_NAME_ONLY,
        taskData="v4tech/imagemagick montage -geometry 450 " + files,
        outputs=[
            TaskOutput(TaskOutputSource.WORKER_DIRECTORY, filePattern=montage_picture_file),
            TaskOutput(TaskOutputSource.PROCESS_OUTPUT, uploadOnFailed=True)
        ]
    )
])
markdown("Added TASKS to", link_entity(url, work_requirement))

# %% [markdown]
# # Wait for the Work Requirement to finish


#%%

def on_update(work_req: WorkRequirement):
    completed = 0
    total = 0
    for task_group in work_req.taskGroups:
        completed += task_group.taskSummary.statusCounts[TaskStatus.COMPLETED]
        total += task_group.taskSummary.taskCount

    markdown(f"WORK REQUIREMENT is {work_req.status} with {completed}/{total} COMPLETED TASKS")


markdown("Waiting for WORK REQUIREMENT to complete...")
listener = DelegatedSubscriptionEventListener(on_update, lambda e: None, lambda: None)
client.work_client.add_work_requirement_listener(work_requirement, listener)
work_requirement = client.work_client.get_work_requirement_helper(work_requirement)\
    .when_requirement_matches(lambda wr: wr.status.is_finished())\
    .result()
client.work_client.remove_work_requirement_listener(listener)
if work_requirement.status != WorkRequirementStatus.COMPLETED:
    raise Exception("WORK REQUIREMENT did not complete. Status " + str(work_requirement.status))

# %% [markdown]
# # Download result of Work Requirement

# %%

output_path = "out"
if not os.path.exists(output_path):
    os.makedirs(output_path)

markdown("Waiting for output picture to download from Object Store...")
output_object = f"{work_requirement.name}/{montage_task_group_name}/{montage_task_name}/{montage_picture_file}"
session = client.object_store_client\
    .create_download_session(namespace, output_object, output_path)
session.start()
session = session.when_status_matches(lambda status: status.is_finished()).result()

if session.status != FileTransferStatus.Completed:
    raise Exception("Output picture failed to download. Status: " + session)

stats = session.get_statistics()
markdown(f"Download {session.status.name.lower()} ({stats.bytes_transferred}B downloaded)")

markdown(image(os.path.join(output_path, output_object), "Output picture"))

client.close()