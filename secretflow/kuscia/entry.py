# Copyright 2023 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import subprocess
import sys
from typing import List

import click
from google.protobuf import json_format
from google.protobuf.json_format import MessageToJson
from kuscia.proto.api.v1alpha1.common_pb2 import DataColumn
from kuscia.proto.api.v1alpha1.datamesh.domaindata_pb2 import DomainData

from secretflow.component.entry import comp_eval, get_comp_def
from secretflow.kuscia.datamesh import (
    create_domain_data,
    create_domain_data_service_stub,
    create_domain_data_source_service_stub,
    get_domain_data,
    get_domain_data_source,
)
from secretflow.kuscia.ray_config import RayConfig
from secretflow.kuscia.sf_config import get_sf_cluster_config
from secretflow.kuscia.task_config import KusciaTaskConfig
from secretflow.spec.v1.data_pb2 import (
    DistData,
    IndividualTable,
    StorageConfig,
    VerticalTable,
)
from secretflow.spec.v1.evaluation_pb2 import NodeEvalParam, NodeEvalResult

_LOG_FORMAT = "%(asctime)s|{}|%(levelname)s|secretflow|%(filename)s:%(funcName)s:%(lineno)d| %(message)s"

DEFAULT_DATAMESH_ADDRESS = "datamesh:8071"


datasource_id = None


def start_ray(ray_conf: RayConfig):
    logging.info(f"ray_conf: {ray_conf}")

    ray_cmd = ray_conf.generate_ray_cmd()

    logging.info(
        f"Trying to start ray head node at {ray_conf.ray_node_ip_address}, start command: {ray_cmd}"
    )

    if not ray_cmd:
        # Local mode, do nothing here.
        return

    process = subprocess.run(ray_cmd, capture_output=True, shell=True)

    if process.returncode != 0:
        err_msg = f"Failed to start ray head node, start command: {ray_cmd}, stderr: {process.stderr}"
        logging.critical(err_msg)
        logging.critical("This process will exit now!")
        sys.exit(-1)
    else:
        if process.stdout:
            logging.info(process.stdout.decode(errors='ignore'))
        logging.info(
            f"Succeeded to start ray head node at {ray_conf.ray_node_ip_address}."
        )


def preprocess_sf_node_eval_param(
    param: NodeEvalParam,
    datamesh_addr: str,
    sf_input_ids: List[str] = None,
    sf_output_uris: List[str] = None,
) -> NodeEvalParam:
    global datasource_id

    comp_def = get_comp_def(param.domain, param.name, param.version)

    assert len(comp_def.inputs) == len(
        sf_input_ids
    ), "cnt of sf_input_ids doesn't match cnt of comp_def.inputs."
    assert len(comp_def.outputs) == len(
        sf_output_uris
    ), "cnt of sf_output_uris doesn't match cnt of comp_def.outputs."

    # get input DistData from GRM
    if len(sf_input_ids):
        param.ClearField('inputs')
        stub = create_domain_data_service_stub(datamesh_addr)
        for id, input_def in zip(sf_input_ids, list(comp_def.inputs)):
            domain_data = get_domain_data(stub, id)

            if datasource_id is not None and domain_data.datasource_id != datasource_id:
                raise RuntimeError(
                    f"datasource_id of domain_data [{domain_data.domaindata_id}] is {domain_data.datasource_id}, which doesn't match global datasource_id {datasource_id}"
                )

            datasource_id = domain_data.datasource_id

            if domain_data.attributes["dist_data"]:
                dist_data = json_format.Parse(
                    domain_data.attributes["dist_data"], DistData()
                )
                param.inputs.append(dist_data)
            else:
                assert "sf.table.individual" in set(input_def.types)

                param.inputs.append(
                    convert_domain_data_to_individual_table(domain_data)
                )

    if len(sf_output_uris):
        param.ClearField('output_uris')
        param.output_uris.extend(sf_output_uris)

    return param


def convert_domain_data_to_individual_table(
    domain_data: DomainData,
) -> IndividualTable:
    logging.warning(
        'kuscia adapter has to deduce dist data from domain data at this moment.'
    )
    assert domain_data.type == 'table'
    dist_data = DistData(name=domain_data.name, type="sf.table.individual")

    meta = IndividualTable()
    for col in domain_data.columns:
        if not col.comment or col.comment == 'feature':
            meta.schema.features.append(col.name)
            meta.schema.feature_types.append(col.type)
        elif col.comment == 'id':
            meta.schema.ids.append(col.name)
            meta.schema.id_types.append(col.type)
        elif col.comment == 'label':
            meta.schema.labels.append(col.name)
            meta.schema.label_types.append(col.type)
    meta.line_count = -1
    dist_data.meta.Pack(meta)

    data_ref = DistData.DataRef()
    data_ref.uri = domain_data.relative_uri
    data_ref.party = domain_data.author
    data_ref.format = 'csv'
    dist_data.data_refs.append(data_ref)

    return dist_data


def convert_dist_data_to_domain_data(
    id: str, x: DistData, output_uri: str, party: str
) -> DomainData:
    global datasource_id

    def convert_data_type(dist_data_type: str) -> str:
        if "table" in dist_data_type:
            return "table"
        elif "model" in dist_data_type:
            return "model"
        elif "rule" in dist_data_type:
            return "rule"
        elif "report" in dist_data_type:
            return "report"
        return "unknown"

    def get_data_columns(x: DistData, party: str) -> List[DataColumn]:
        ret = []
        if x.type == "sf.table.individual" or x.type == "sf.table.vertical_table":
            meta = (
                IndividualTable()
                if x.type.lower() == "sf.table.individual"
                else VerticalTable()
            )

            assert x.meta.Unpack(meta)

            schemas = (
                [meta.schema]
                if x.type.lower() == "sf.table.individual"
                else meta.schemas
            )

            for schema, data_ref in zip(schemas, list(x.data_refs)):
                if data_ref.party != party:
                    continue
                for id, type in zip(list(schema.ids), list(schema.id_types)):
                    ret.append(DataColumn(name=id, type=type, comment="id"))

                for feature, type in zip(
                    list(schema.features), list(schema.feature_types)
                ):
                    ret.append(DataColumn(name=feature, type=type, comment="feature"))

                for label, type in zip(list(schema.labels), list(schema.label_types)):
                    ret.append(DataColumn(name=label, type=type, comment="label"))

        return ret

    domain_data = DomainData(
        domaindata_id=id,
        name=x.name,
        type=convert_data_type(x.type),
        relative_uri=output_uri,
        datasource_id=datasource_id,
        vendor="secretflow",
    )

    domain_data.attributes["dist_data"] = MessageToJson(
        x, including_default_value_fields=True
    )
    domain_data.columns.extend(get_data_columns(x, party))

    return domain_data


def postprocess_sf_node_eval_result(
    res: NodeEvalResult,
    datamesh_addr: str,
    party: str,
    sf_output_ids: List[str] = None,
    sf_output_uris: List[str] = None,
) -> None:
    global datasource_id

    # write output DistData to GRM
    if sf_output_ids is not None and len(sf_output_ids) > 0:
        if datasource_id is None:
            raise RuntimeError(f"datasource_id is missing.")

        stub = create_domain_data_service_stub(datamesh_addr)
        for domain_data_id, dist_data, output_uri in zip(
            sf_output_ids, res.outputs, sf_output_uris
        ):
            domain_data = convert_dist_data_to_domain_data(
                domain_data_id, dist_data, output_uri, party
            )
            create_domain_data(stub, domain_data)


def try_to_get_datasource_id(task_conf: KusciaTaskConfig):
    global datasource_id
    party_name = task_conf.party_name
    sf_datasource_config = task_conf.sf_datasource_config
    if sf_datasource_config is not None:
        if party_name not in sf_datasource_config:
            raise RuntimeError(
                f"party {party_name} is missing in sf_datasource_config."
            )
        datasource_id = sf_datasource_config[party_name]["id"]


def get_storage_config(
    kuscia_config: KusciaTaskConfig,
    datamesh_addr: str,
    datasource_id: str = None,
) -> StorageConfig:
    party_id = kuscia_config.task_cluster_def.self_party_idx
    party_name = kuscia_config.task_cluster_def.parties[party_id].name
    kuscia_record = kuscia_config.sf_storage_config

    if (
        kuscia_record is not None
        and party_name not in kuscia_record
        and datasource_id is None
    ):
        raise RuntimeError(
            f"storage config of party [{party_name}] is missing. It must be provided with sf_storage_config explicitly or be inferred from sf_input_ids with DataMesh services."
        )

    if kuscia_record is not None and party_name in kuscia_record:
        storage_config = kuscia_record[party_name]
    else:
        # try to get storage config with sf_datasource_config
        stub = create_domain_data_source_service_stub(datamesh_addr)
        domain_data_source = get_domain_data_source(stub, datasource_id)

        storage_config = StorageConfig(
            type="local_fs",
            local_fs=StorageConfig.LocalFSConfig(
                wd=domain_data_source.info.localfs.path
            ),
        )

    return storage_config


@click.command()
@click.argument("task_config_path", type=click.Path(exists=True))
@click.option("--datamesh_addr", required=False, default=DEFAULT_DATAMESH_ADDRESS)
def main(task_config_path, datamesh_addr):
    task_conf = KusciaTaskConfig.from_file(task_config_path)

    try_to_get_datasource_id(task_conf)

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.DEBUG,
        format=_LOG_FORMAT.format(task_conf.party_name),
        force=True,
    )

    ray_config = RayConfig.from_kuscia_task_config(task_conf)
    start_ray(ray_config)

    sf_node_eval_param = preprocess_sf_node_eval_param(
        task_conf.sf_node_eval_param,
        datamesh_addr,
        task_conf.sf_input_ids,
        task_conf.sf_output_uris,
    )

    sf_cluster_config = get_sf_cluster_config(task_conf)

    storage_config = get_storage_config(task_conf, datamesh_addr, datasource_id)

    res = comp_eval(sf_node_eval_param, storage_config, sf_cluster_config)

    postprocess_sf_node_eval_result(
        res,
        datamesh_addr,
        task_conf.party_name,
        task_conf.sf_output_ids,
        task_conf.sf_output_uris,
    )

    logging.info("Succeeded to run component.")

    sys.exit(0)


if __name__ == "__main__":
    main()
