#!/usr/bin/env ambari-python-wrap
"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from resource_management.core.logger import Logger
import json
import re
from resource_management.libraries.functions import format


class HDP26StackAdvisor(HDP25StackAdvisor):
  def __init__(self):
      super(HDP26StackAdvisor, self).__init__()
      Logger.initialize_logger()

  def getServiceConfigurationRecommenderDict(self):
      parentRecommendConfDict = super(HDP26StackAdvisor, self).getServiceConfigurationRecommenderDict()
      childRecommendConfDict = {
        "DRUID": self.recommendDruidConfigurations,
        "ATLAS": self.recommendAtlasConfigurations,
        "TEZ": self.recommendTezConfigurations,
        "RANGER": self.recommendRangerConfigurations,
        "RANGER_KMS": self.recommendRangerKMSConfigurations,
        "HDFS": self.recommendHDFSConfigurations,
        "HIVE": self.recommendHIVEConfigurations,
        "HBASE": self.recommendHBASEConfigurations,
        "YARN": self.recommendYARNConfigurations,
        "KAFKA": self.recommendKAFKAConfigurations
      }
      parentRecommendConfDict.update(childRecommendConfDict)
      return parentRecommendConfDict

  def recommendAtlasConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendAtlasConfigurations(configurations, clusterData, services, hosts)
    servicesList = [service["StackServices"]["service_name"] for service in services["services"]]
    putAtlasApplicationProperty = self.putProperty(configurations, "application-properties", services)

    knox_host = 'localhost'
    knox_port = '8443'
    if 'KNOX' in servicesList:
      knox_hosts = self.getComponentHostNames(services, "KNOX", "KNOX_GATEWAY")
      if len(knox_hosts) > 0:
        knox_hosts.sort()
        knox_host = knox_hosts[0]
      if 'gateway-site' in services['configurations'] and 'gateway.port' in services['configurations']["gateway-site"]["properties"]:
        knox_port = services['configurations']["gateway-site"]["properties"]['gateway.port']
      putAtlasApplicationProperty('atlas.sso.knox.providerurl', 'https://{0}:{1}/gateway/knoxsso/api/v1/websso'.format(knox_host, knox_port))

  def recommendDruidConfigurations(self, configurations, clusterData, services, hosts):

      # druid is not in list of services to be installed
      if 'druid-common' not in services['configurations']:
        return

      componentsListList = [service["components"] for service in services["services"]]
      componentsList = [item["StackServiceComponents"] for sublist in componentsListList for item in sublist]
      servicesList = [service["StackServices"]["service_name"] for service in services["services"]]
      putCommonProperty = self.putProperty(configurations, "druid-common", services)

      putCommonProperty('druid.zk.service.host', self.getZKHostPortString(services))
      self.recommendDruidMaxMemoryLimitConfigurations(configurations, clusterData, services, hosts)

      # recommending the metadata storage uri
      database_name = services['configurations']["druid-common"]["properties"]["database_name"]
      metastore_hostname = services['configurations']["druid-common"]["properties"]["metastore_hostname"]
      database_type = services['configurations']["druid-common"]["properties"]["druid.metadata.storage.type"]
      metadata_storage_port = "1527"
      mysql_module_name = "mysql-metadata-storage"
      postgres_module_name = "postgresql-metadata-storage"
      extensions_load_list = services['configurations']['druid-common']['properties']['druid.extensions.loadList']
      putDruidCommonProperty = self.putProperty(configurations, "druid-common", services)

      extensions_load_list = self.removeFromList(extensions_load_list, mysql_module_name)
      extensions_load_list = self.removeFromList(extensions_load_list, postgres_module_name)

      if database_type == 'mysql':
          metadata_storage_port = "3306"
          extensions_load_list = self.addToList(extensions_load_list, mysql_module_name)

      if database_type == 'postgresql':
          extensions_load_list = self.addToList(extensions_load_list, postgres_module_name)
          metadata_storage_port = "5432"

      putDruidCommonProperty('druid.metadata.storage.connector.port', metadata_storage_port)
      putDruidCommonProperty('druid.metadata.storage.connector.connectURI',
                             self.getMetadataConnectionString(database_type).format(metastore_hostname, database_name,
                                                                                    metadata_storage_port))
      # HDFS is installed
      if "HDFS" in servicesList and "hdfs-site" in services["configurations"]:
          # recommend HDFS as default deep storage
          extensions_load_list = self.addToList(extensions_load_list, "druid-hdfs-storage")
          putCommonProperty("druid.storage.type", "hdfs")
          putCommonProperty("druid.storage.storageDirectory", "/user/druid/data")
          # configure indexer logs configs
          putCommonProperty("druid.indexer.logs.type", "hdfs")
          putCommonProperty("druid.indexer.logs.directory", "/user/druid/logs")

      if "KAFKA" in servicesList:
          extensions_load_list = self.addToList(extensions_load_list, "druid-kafka-indexing-service")

      if 'AMBARI_METRICS' in servicesList:
        extensions_load_list = self.addToList(extensions_load_list, "ambari-metrics-emitter")

      putCommonProperty('druid.extensions.loadList', extensions_load_list)

      # JVM Configs go to env properties
      putEnvProperty = self.putProperty(configurations, "druid-env", services)

      # processing thread pool Config
      for component in ['DRUID_HISTORICAL', 'DRUID_BROKER']:
          component_hosts = self.getHostsWithComponent("DRUID", component, services, hosts)
          nodeType = self.DRUID_COMPONENT_NODE_TYPE_MAP[component]
          putComponentProperty = self.putProperty(configurations, format("druid-{nodeType}"), services)
          if (component_hosts is not None and len(component_hosts) > 0):
              totalAvailableCpu = self.getMinCpu(component_hosts)
              processingThreads = 1
              if totalAvailableCpu > 1:
                  processingThreads = totalAvailableCpu - 1
              putComponentProperty('druid.processing.numThreads', processingThreads)
              putComponentProperty('druid.server.http.numThreads', max(10, (totalAvailableCpu * 17) / 16 + 2) + 30)

      # superset is in list of services to be installed
      if 'druid-superset' in services['configurations']:
        # Recommendations for Superset
        superset_database_type = services['configurations']["druid-superset"]["properties"]["SUPERSET_DATABASE_TYPE"]
        putSupersetProperty = self.putProperty(configurations, "druid-superset", services)

        if superset_database_type == "mysql":
            putSupersetProperty("SUPERSET_DATABASE_PORT", "3306")
        elif superset_database_type == "postgresql":
            putSupersetProperty("SUPERSET_DATABASE_PORT", "5432")

  def recommendYARNConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendYARNConfigurations(configurations, clusterData, services, hosts)
    putYarnSiteProperty = self.putProperty(configurations, "yarn-site", services)

    if "yarn-site" in services["configurations"] and \
                    "yarn.resourcemanager.scheduler.monitor.enable" in services["configurations"]["yarn-site"]["properties"]:
      scheduler_monitor_enabled = services["configurations"]["yarn-site"]["properties"]["yarn.resourcemanager.scheduler.monitor.enable"]
      if scheduler_monitor_enabled.lower() == 'true':
        putYarnSiteProperty('yarn.scheduler.capacity.ordering-policy.priority-utilization.underutilized-preemption.enabled', "true")
      else:
        putYarnSiteProperty('yarn.scheduler.capacity.ordering-policy.priority-utilization.underutilized-preemption.enabled', "false")

    if 'yarn-env' in services['configurations'] and 'yarn_user' in services['configurations']['yarn-env']['properties']:
      yarn_user = services['configurations']['yarn-env']['properties']['yarn_user']
    else:
      yarn_user = 'yarn'
    if 'ranger-yarn-plugin-properties' in configurations and 'ranger-yarn-plugin-enabled' in configurations['ranger-yarn-plugin-properties']['properties']:
      ranger_yarn_plugin_enabled = (configurations['ranger-yarn-plugin-properties']['properties']['ranger-yarn-plugin-enabled'].lower() == 'Yes'.lower())
    elif 'ranger-yarn-plugin-properties' in services['configurations'] and 'ranger-yarn-plugin-enabled' in services['configurations']['ranger-yarn-plugin-properties']['properties']:
      ranger_yarn_plugin_enabled = (services['configurations']['ranger-yarn-plugin-properties']['properties']['ranger-yarn-plugin-enabled'].lower() == 'Yes'.lower())
    else:
      ranger_yarn_plugin_enabled = False

    #yarn timeline service url depends on http policy and takes the host name of the yarn webapp.
    if "yarn-site" in services["configurations"] and \
                    "yarn.timeline-service.webapp.https.address" in services["configurations"]["yarn-site"]["properties"] and \
                    "yarn.http.policy" in services["configurations"]["yarn-site"]["properties"] and \
                    "yarn.log.server.web-service.url" in services["configurations"]["yarn-site"]["properties"]:
        if services["configurations"]["yarn-site"]["properties"]["yarn.http.policy"] == 'HTTP_ONLY':
            webapp_address = services["configurations"]["yarn-site"]["properties"]["yarn.timeline-service.webapp.address"]
            webservice_url = "http://"+webapp_address+"/ws/v1/applicationhistory"
        else:
            webapp_address = services["configurations"]["yarn-site"]["properties"]["yarn.timeline-service.webapp.https.address"]
            webservice_url = "https://"+webapp_address+"/ws/v1/applicationhistory"
        putYarnSiteProperty('yarn.log.server.web-service.url',webservice_url )

    if ranger_yarn_plugin_enabled and 'ranger-yarn-plugin-properties' in services['configurations'] and 'REPOSITORY_CONFIG_USERNAME' in services['configurations']['ranger-yarn-plugin-properties']['properties']:
      Logger.info("Setting Yarn Repo user for Ranger.")
      putRangerYarnPluginProperty = self.putProperty(configurations, "ranger-yarn-plugin-properties", services)
      putRangerYarnPluginProperty("REPOSITORY_CONFIG_USERNAME",yarn_user)
    else:
      Logger.info("Not setting Yarn Repo user for Ranger.")

  def getMetadataConnectionString(self, database_type):
      driverDict = {
          'mysql': 'jdbc:mysql://{0}:{2}/{1}?createDatabaseIfNotExist=true',
          'derby': 'jdbc:derby://{0}:{2}/{1};create=true',
          'postgresql': 'jdbc:postgresql://{0}:{2}/{1}'
      }
      return driverDict.get(database_type.lower())

  def addToList(self, json_list, word):
      desr_list = json.loads(json_list)
      if word not in desr_list:
          desr_list.append(word)
      return json.dumps(desr_list)

  def removeFromList(self, json_list, word):
      desr_list = json.loads(json_list)
      if word in desr_list:
          desr_list.remove(word)
      return json.dumps(desr_list)

  def recommendDruidMaxMemoryLimitConfigurations(self, configurations, clusterData, services, hosts):
      putEnvPropertyAttribute = self.putPropertyAttribute(configurations, "druid-env")
      for component in ["DRUID_HISTORICAL", "DRUID_MIDDLEMANAGER", "DRUID_BROKER", "DRUID_OVERLORD",
                        "DRUID_COORDINATOR"]:
          component_hosts = self.getHostsWithComponent("DRUID", component, services, hosts)
          if component_hosts is not None and len(component_hosts) > 0:
              totalAvailableMem = self.getMinMemory(component_hosts) / 1024  # In MB
              nodeType = self.DRUID_COMPONENT_NODE_TYPE_MAP[component]
              putEnvPropertyAttribute(format('druid.{nodeType}.jvm.heap.memory'), 'maximum',
                                      max(totalAvailableMem, 1024))

  DRUID_COMPONENT_NODE_TYPE_MAP = {
      'DRUID_BROKER': 'broker',
      'DRUID_COORDINATOR': 'coordinator',
      'DRUID_HISTORICAL': 'historical',
      'DRUID_MIDDLEMANAGER': 'middlemanager',
      'DRUID_OVERLORD': 'overlord',
      'DRUID_ROUTER': 'router'
  }

  def getMinMemory(self, component_hosts):
      min_ram_kb = 1073741824  # 1 TB
      for host in component_hosts:
          ram_kb = host['Hosts']['total_mem']
          min_ram_kb = min(min_ram_kb, ram_kb)
      return min_ram_kb

  def getMinCpu(self, component_hosts):
      min_cpu = 256
      for host in component_hosts:
          cpu_count = host['Hosts']['cpu_count']
          min_cpu = min(min_cpu, cpu_count)
      return min_cpu

  def getServiceConfigurationValidators(self):
      parentValidators = super(HDP26StackAdvisor, self).getServiceConfigurationValidators()
      childValidators = {
          "DRUID": {"druid-env": self.validateDruidEnvConfigurations,
                    "druid-historical": self.validateDruidHistoricalConfigurations,
                    "druid-broker": self.validateDruidBrokerConfigurations},
          "RANGER": {"ranger-ugsync-site": self.validateRangerUsersyncConfigurations},
          "YARN" : {"yarn-site": self.validateYarnSiteConfigurations}
      }
      self.mergeValidators(parentValidators, childValidators)
      return parentValidators

  def validateDruidEnvConfigurations(self, properties, recommendedDefaults, configurations, services, hosts):
      validationItems = []
      #  Minimum Direct memory Validation
      envProperties = services['configurations']['druid-env']['properties']
      for nodeType in ['broker', 'historical']:
          properties = services['configurations'][format('druid-{nodeType}')]['properties']
          intermediateBufferSize = int(properties['druid.processing.buffer.sizeBytes']) / (1024 * 1024)  # In MBs
          processingThreads = int(properties['druid.processing.numThreads'])
          directMemory = int(envProperties[format('druid.{nodeType}.jvm.direct.memory')])
          if directMemory < (processingThreads + 1) * intermediateBufferSize:
              validationItems.extend(
                  {"config-name": format("druid.{nodeType}.jvm.direct.memory"), "item": self.getErrorItem(
                      format(
                          "Not enough direct memory available for {nodeType} Node."
                          "Please adjust druid.{nodeType}.jvm.direct.memory, druid.processing.buffer.sizeBytes, druid.processing.numThreads"
                      )
                  )
                   })
      return self.toConfigurationValidationProblems(validationItems, "druid-env")

  def validateYarnSiteConfigurations(self, properties, recommendedDefaults, configurations, services, hosts):
      validationItems = []
      siteProperties = services["configurations"]["yarn-site"]["properties"]
      if services["configurations"]["yarn-site"]["properties"]["yarn.http.policy"] == 'HTTP_ONLY':
         webapp_address = services["configurations"]["yarn-site"]["properties"]["yarn.timeline-service.webapp.address"]
         propertyValue = "http://"+webapp_address+"/ws/v1/applicationhistory"
      else:
         webapp_address = services["configurations"]["yarn-site"]["properties"]["yarn.timeline-service.webapp.https.address"]
         propertyValue = "https://"+webapp_address+"/ws/v1/applicationhistory"
      Logger.info("validateYarnSiteConfigurations: recommended value for webservice url"+services["configurations"]["yarn-site"]["properties"]["yarn.log.server.web-service.url"])
      if services["configurations"]["yarn-site"]["properties"]["yarn.log.server.web-service.url"] != propertyValue:
         validationItems = [
              {"config-name": "yarn.log.server.web-service.url",
               "item": self.getWarnItem("Value should be %s" % propertyValue)}]
      return self.toConfigurationValidationProblems(validationItems, "yarn-site")

  def validateDruidHistoricalConfigurations(self, properties, recommendedDefaults, configurations, services, hosts):
      validationItems = [
          {"config-name": "druid.processing.numThreads",
           "item": self.validatorEqualsToRecommendedItem(properties, recommendedDefaults,
                                                         "druid.processing.numThreads")}
      ]
      return self.toConfigurationValidationProblems(validationItems, "druid-historical")

  def validateDruidBrokerConfigurations(self, properties, recommendedDefaults, configurations, services, hosts):
        validationItems = [
            {"config-name": "druid.processing.numThreads",
             "item": self.validatorEqualsToRecommendedItem(properties, recommendedDefaults,
                                                           "druid.processing.numThreads")}
        ]
        return self.toConfigurationValidationProblems(validationItems, "druid-broker")

  def recommendTezConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendTezConfigurations(configurations, clusterData, services, hosts)
    putTezProperty = self.putProperty(configurations, "tez-site")

    # TEZ JVM options
    jvmGCParams = "-XX:+UseParallelGC"
    if "ambari-server-properties" in services and "java.home" in services["ambari-server-properties"]:
      # JDK8 needs different parameters
      match = re.match(".*\/jdk(1\.\d+)[\-\_\.][^/]*$", services["ambari-server-properties"]["java.home"])
      if match and len(match.groups()) > 0:
        # Is version >= 1.8
        versionSplits = re.split("\.", match.group(1))
        if versionSplits and len(versionSplits) > 1 and int(versionSplits[0]) > 0 and int(versionSplits[1]) > 7:
          jvmGCParams = "-XX:+UseG1GC -XX:+ResizeTLAB"
    tez_jvm_opts = "-XX:+PrintGCDetails -verbose:gc -XX:+PrintGCTimeStamps -XX:+UseNUMA "
    # Append 'jvmGCParams' and 'Heap Dump related option' (({{heap_dump_opts}}) Expanded while writing the
    # configurations at start/restart time).
    tez_jvm_updated_opts = tez_jvm_opts + jvmGCParams + "{{heap_dump_opts}}"
    putTezProperty('tez.am.launch.cmd-opts', tez_jvm_updated_opts)
    putTezProperty('tez.task.launch.cmd-opts', tez_jvm_updated_opts)
    Logger.info("Updated 'tez-site' config 'tez.task.launch.cmd-opts' and 'tez.am.launch.cmd-opts' as "
                ": {0}".format(tez_jvm_updated_opts))

  def recommendRangerConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendRangerConfigurations(configurations, clusterData, services, hosts)

    putRangerUgsyncSite = self.putProperty(configurations, 'ranger-ugsync-site', services)

    delta_sync_enabled = False
    if 'ranger-ugsync-site' in services['configurations'] and 'ranger.usersync.ldap.deltasync' in services['configurations']['ranger-ugsync-site']['properties']:
      delta_sync_enabled = services['configurations']['ranger-ugsync-site']['properties']['ranger.usersync.ldap.deltasync'] == "true"

    if delta_sync_enabled:
      putRangerUgsyncSite("ranger.usersync.group.searchenabled", "true")
    else:
      putRangerUgsyncSite("ranger.usersync.group.searchenabled", "false")

  def validateRangerUsersyncConfigurations(self, properties, recommendedDefaults, configurations, services, hosts):
    ranger_usersync_properties = properties
    validationItems = []

    delta_sync_enabled = 'ranger.usersync.ldap.deltasync' in ranger_usersync_properties \
      and ranger_usersync_properties['ranger.usersync.ldap.deltasync'].lower() == 'true'
    group_sync_enabled = 'ranger.usersync.group.searchenabled' in ranger_usersync_properties \
      and ranger_usersync_properties['ranger.usersync.group.searchenabled'].lower() == 'true'

    if delta_sync_enabled and not group_sync_enabled:
      validationItems.append({"config-name": "ranger.usersync.group.searchenabled",
                            "item": self.getWarnItem(
                            "Need to set ranger.usersync.group.searchenabled as true, as ranger.usersync.ldap.deltasync is enabled")})

    return self.toConfigurationValidationProblems(validationItems, "ranger-ugsync-site")

  def recommendRangerKMSConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendRangerKMSConfigurations(configurations, clusterData, services, hosts)
    putRangerKmsEnvProperty = self.putProperty(configurations, "kms-env", services)

    ranger_kms_ssl_enabled = False
    ranger_kms_ssl_port = "9393"
    if 'ranger-kms-site' in services['configurations'] and 'ranger.service.https.attrib.ssl.enabled' in services['configurations']['ranger-kms-site']['properties']:
      ranger_kms_ssl_enabled = services['configurations']['ranger-kms-site']['properties']['ranger.service.https.attrib.ssl.enabled'].lower() == "true"

    if 'ranger-kms-site' in services['configurations'] and 'ranger.service.https.port' in services['configurations']['ranger-kms-site']['properties']:
      ranger_kms_ssl_port = services['configurations']['ranger-kms-site']['properties']['ranger.service.https.port']

    if ranger_kms_ssl_enabled:
      putRangerKmsEnvProperty("kms_port", ranger_kms_ssl_port)
    else:
      putRangerKmsEnvProperty("kms_port", "9292")

  def recommendHDFSConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendHDFSConfigurations(configurations, clusterData, services, hosts)
    if 'hadoop-env' in services['configurations'] and 'hdfs_user' in  services['configurations']['hadoop-env']['properties']:
      hdfs_user = services['configurations']['hadoop-env']['properties']['hdfs_user']
    else:
      hdfs_user = 'hadoop'

    if 'ranger-hdfs-plugin-properties' in configurations and 'ranger-hdfs-plugin-enabled' in configurations['ranger-hdfs-plugin-properties']['properties']:
      ranger_hdfs_plugin_enabled = (configurations['ranger-hdfs-plugin-properties']['properties']['ranger-hdfs-plugin-enabled'].lower() == 'Yes'.lower())
    elif 'ranger-hdfs-plugin-properties' in services['configurations'] and 'ranger-hdfs-plugin-enabled' in services['configurations']['ranger-hdfs-plugin-properties']['properties']:
      ranger_hdfs_plugin_enabled = (services['configurations']['ranger-hdfs-plugin-properties']['properties']['ranger-hdfs-plugin-enabled'].lower() == 'Yes'.lower())
    else:
      ranger_hdfs_plugin_enabled = False

    if ranger_hdfs_plugin_enabled and 'ranger-hdfs-plugin-properties' in services['configurations'] and 'REPOSITORY_CONFIG_USERNAME' in services['configurations']['ranger-hdfs-plugin-properties']['properties']:
      Logger.info("Setting HDFS Repo user for Ranger.")
      putRangerHDFSPluginProperty = self.putProperty(configurations, "ranger-hdfs-plugin-properties", services)
      putRangerHDFSPluginProperty("REPOSITORY_CONFIG_USERNAME",hdfs_user)
    else:
      Logger.info("Not setting HDFS Repo user for Ranger.")

  def recommendHIVEConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendHIVEConfigurations(configurations, clusterData, services, hosts)
    if 'hive-env' in services['configurations'] and 'hive_user' in services['configurations']['hive-env']['properties']:
      hive_user = services['configurations']['hive-env']['properties']['hive_user']
    else:
      hive_user = 'hive'

    if 'hive-env' in configurations and 'hive_security_authorization' in configurations['hive-env']['properties']:
      ranger_hive_plugin_enabled = (configurations['hive-env']['properties']['hive_security_authorization'].lower() == 'ranger')
    elif 'hive-env' in services['configurations'] and 'hive_security_authorization' in services['configurations']['hive-env']['properties']:
      ranger_hive_plugin_enabled = (services['configurations']['hive-env']['properties']['hive_security_authorization'].lower() == 'ranger')
    else :
      ranger_hive_plugin_enabled = False

    if ranger_hive_plugin_enabled and 'ranger-hive-plugin-properties' in services['configurations'] and 'REPOSITORY_CONFIG_USERNAME' in services['configurations']['ranger-hive-plugin-properties']['properties']:
      Logger.info("Setting Hive Repo user for Ranger.")
      putRangerHivePluginProperty = self.putProperty(configurations, "ranger-hive-plugin-properties", services)
      putRangerHivePluginProperty("REPOSITORY_CONFIG_USERNAME",hive_user)
    else:
      Logger.info("Not setting Hive Repo user for Ranger.")

  def recommendHBASEConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendHBASEConfigurations(configurations, clusterData, services, hosts)
    if 'hbase-env' in services['configurations'] and 'hbase_user' in services['configurations']['hbase-env']['properties']:
      hbase_user = services['configurations']['hbase-env']['properties']['hbase_user']
    else:
      hbase_user = 'hbase'

    if 'ranger-hbase-plugin-properties' in configurations and 'ranger-hbase-plugin-enabled' in configurations['ranger-hbase-plugin-properties']['properties']:
      ranger_hbase_plugin_enabled = (configurations['ranger-hbase-plugin-properties']['properties']['ranger-hbase-plugin-enabled'].lower() == 'Yes'.lower())
    elif 'ranger-hbase-plugin-properties' in services['configurations'] and 'ranger-hbase-plugin-enabled' in services['configurations']['ranger-hbase-plugin-properties']['properties']:
      ranger_hbase_plugin_enabled = (services['configurations']['ranger-hbase-plugin-properties']['properties']['ranger-hbase-plugin-enabled'].lower() == 'Yes'.lower())
    else:
      ranger_hbase_plugin_enabled = False

    if ranger_hbase_plugin_enabled and 'ranger-hbase-plugin-properties' in services['configurations'] and 'REPOSITORY_CONFIG_USERNAME' in services['configurations']['ranger-hbase-plugin-properties']['properties']:
      Logger.info("Setting Hbase Repo user for Ranger.")
      putRangerHbasePluginProperty = self.putProperty(configurations, "ranger-hbase-plugin-properties", services)
      putRangerHbasePluginProperty("REPOSITORY_CONFIG_USERNAME",hbase_user)
    else:
      Logger.info("Not setting Hbase Repo user for Ranger.")

  def recommendKAFKAConfigurations(self, configurations, clusterData, services, hosts):
    super(HDP26StackAdvisor, self).recommendKAFKAConfigurations(configurations, clusterData, services, hosts)
    if 'kafka-env' in services['configurations'] and 'kafka_user' in services['configurations']['kafka-env']['properties']:
      kafka_user = services['configurations']['kafka-env']['properties']['kafka_user']
    else:
      kafka_user = "kafka"

    if 'ranger-kafka-plugin-properties' in configurations and  'ranger-kafka-plugin-enabled' in configurations['ranger-kafka-plugin-properties']['properties']:
      ranger_kafka_plugin_enabled = (configurations['ranger-kafka-plugin-properties']['properties']['ranger-kafka-plugin-enabled'].lower() == 'Yes'.lower())
    elif 'ranger-kafka-plugin-properties' in services['configurations'] and 'ranger-kafka-plugin-enabled' in services['configurations']['ranger-kafka-plugin-properties']['properties']:
      ranger_kafka_plugin_enabled = (services['configurations']['ranger-kafka-plugin-properties']['properties']['ranger-kafka-plugin-enabled'].lower() == 'Yes'.lower())
    else:
      ranger_kafka_plugin_enabled = False

    if ranger_kafka_plugin_enabled and 'ranger-kafka-plugin-properties' in services['configurations'] and 'REPOSITORY_CONFIG_USERNAME' in services['configurations']['ranger-kafka-plugin-properties']['properties']:
      Logger.info("Setting Kafka Repo user for Ranger.")
      putRangerKafkaPluginProperty = self.putProperty(configurations, "ranger-kafka-plugin-properties", services)
      putRangerKafkaPluginProperty("REPOSITORY_CONFIG_USERNAME",kafka_user)
    else:
      Logger.info("Not setting Kafka Repo user for Ranger.")
