/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.ambari.server.controller.metrics.timeline;

import org.apache.ambari.server.configuration.ComponentSSLConfiguration;
import org.apache.ambari.server.controller.internal.PropertyInfo;
import org.apache.ambari.server.controller.metrics.MetricHostProvider;
import org.apache.ambari.server.controller.metrics.MetricsPropertyProvider;
import org.apache.ambari.server.controller.spi.Request;
import org.apache.ambari.server.controller.spi.Resource;
import org.apache.ambari.server.controller.spi.SystemException;
import org.apache.ambari.server.controller.spi.TemporalInfo;
import org.apache.ambari.server.controller.utilities.StreamProvider;
import org.apache.hadoop.metrics2.sink.timeline.TimelineMetric;
import org.apache.hadoop.metrics2.sink.timeline.TimelineMetrics;
import org.apache.http.client.utils.URIBuilder;
import org.codehaus.jackson.map.AnnotationIntrospector;
import org.codehaus.jackson.map.ObjectMapper;
import org.codehaus.jackson.map.ObjectReader;
import org.codehaus.jackson.xc.JaxbAnnotationIntrospector;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.text.DecimalFormat;
import java.util.Collection;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import static org.apache.ambari.server.controller.metrics.MetricsServiceProvider.MetricsService.TIMELINE_METRICS;
import static org.codehaus.jackson.map.annotate.JsonSerialize.Inclusion;

public abstract class AMSPropertyProvider extends MetricsPropertyProvider {
  static final Map<String, String> TIMELINE_APPID_MAP = new HashMap<String, String>();
  private static ObjectMapper mapper;
  private final static ObjectReader timelineObjectReader;
  private static final String METRIC_REGEXP_PATTERN = "\\([^)]*\\)";

  static {
    TIMELINE_APPID_MAP.put("HBASE_MASTER", "HBASE");
    TIMELINE_APPID_MAP.put("HBASE_REGIONSERVER", "HBASE");
    TIMELINE_APPID_MAP.put("METRIC_COLLECTOR", "AMS-HBASE");

    mapper = new ObjectMapper();
    AnnotationIntrospector introspector = new JaxbAnnotationIntrospector();
    mapper.setAnnotationIntrospector(introspector);
    //noinspection deprecation
    mapper.getSerializationConfig().setSerializationInclusion(Inclusion.NON_NULL);
    timelineObjectReader = mapper.reader(TimelineMetrics.class);
  }

  public AMSPropertyProvider(Map<String, Map<String, PropertyInfo>> componentPropertyInfoMap,
                             StreamProvider streamProvider,
                             ComponentSSLConfiguration configuration,
                             MetricHostProvider hostProvider,
                             String clusterNamePropertyId,
                             String hostNamePropertyId,
                             String componentNamePropertyId) {

    super(componentPropertyInfoMap, streamProvider, configuration,
      hostProvider, clusterNamePropertyId, hostNamePropertyId,
      componentNamePropertyId);
  }

  protected String getOverridenComponentName(Resource resource) {
    String componentName = getComponentName(resource);
    // Hack: To allow host queries to succeed
    if (componentName.equals("HOST")) {
      return  "*";
    }
    return componentName;
  }

  /**
   * The information required to make a single call to the Metrics service.
   */
  class MetricsRequest {
    private final TemporalInfo temporalInfo;
    private final Map<String, Set<Resource>> resources = new HashMap<String, Set<Resource>>();
    private final Map<String, Set<String>> metrics = new HashMap<String, Set<String>>();
    private final URIBuilder uriBuilder;
    private final String dummyHostName = "__SummaryInfo__";

    private MetricsRequest(TemporalInfo temporalInfo, URIBuilder uriBuilder) {
      this.temporalInfo = temporalInfo;
      this.uriBuilder = uriBuilder;
    }

    public void putResource(String hostname, Resource resource) {
      if (hostname == null) {
        hostname = dummyHostName;
      }
      Set<Resource> resourceSet = resources.get(hostname);
      if (resourceSet == null) {
        resourceSet = new HashSet<Resource>();
        resources.put(hostname, resourceSet);
      }
      resourceSet.add(resource);
    }

    public void putPropertyId(String metric, String id) {
      Set<String> propertyIds = metrics.get(metric);

      if (propertyIds == null) {
        propertyIds = new HashSet<String>();
        metrics.put(metric, propertyIds);
      }
      propertyIds.add(id);
    }

    /**
     * Populate the associated resources by making a call to the Metrics
     * service.
     *
     * @return a collection of populated resources
     * @throws SystemException if unable to populate the resources
     */
    public Collection<Resource> populateResources() throws SystemException {
      // No open ended query support.
      if (temporalInfo == null || temporalInfo.getStartTime() == null ||
          temporalInfo.getEndTime() == null) {
        return Collections.emptySet();
      }

      for (Map.Entry<String, Set<Resource>> resourceEntry : resources.entrySet()) {
        String hostname = resourceEntry.getKey();
        Set<Resource> resourceSet = resourceEntry.getValue();

        for (Resource resource : resourceSet) {
          String clusterName = (String) resource.getPropertyValue(clusterNamePropertyId);

          // Check liveliness of host
          if (!hostProvider.isCollectorHostLive(clusterName, TIMELINE_METRICS)) {
            LOG.info("METRIC_COLLECTOR host is not live. Skip populating " +
              "resources with metrics.");
            return Collections.emptySet();
          }

          // Check liveliness of Collector
          if (!hostProvider.isCollectorComponentLive(clusterName, TIMELINE_METRICS)) {
            LOG.info("METRIC_COLLECTOR is not live. Skip populating resources" +
              " with metrics.");
            return Collections.emptySet();
          }

          String metricsParam = getSetString(processRegexps(metrics.keySet()), -1);
          // Reuse uriBuilder
          uriBuilder.removeQuery();

          if (metricsParam.length() > 0) {
            uriBuilder.setParameter("metricNames", metricsParam);
          }

          if (hostname != null && !hostname.isEmpty() && !hostname.equals(dummyHostName)) {
            uriBuilder.setParameter("hostname", hostname);
          }

          String componentName = getComponentName(resource);
          if (componentName != null && !componentName.isEmpty()) {
            if (TIMELINE_APPID_MAP.containsKey(componentName)) {
              componentName = TIMELINE_APPID_MAP.get(componentName);
            }
            uriBuilder.setParameter("appId", componentName);
          }

          long startTime = temporalInfo.getStartTime();
          if (startTime != -1) {
            uriBuilder.setParameter("startTime", String.valueOf(startTime));
          }

          long endTime = temporalInfo.getEndTime();
          if (endTime != -1) {
            uriBuilder.setParameter("endTime", String.valueOf(endTime));
          }

          BufferedReader reader = null;
          String spec = uriBuilder.toString();
          try {
            LOG.debug("Metrics request url =" + spec);
            reader = new BufferedReader(new InputStreamReader(streamProvider.readFrom(spec)));

            TimelineMetrics timelineMetrics = timelineObjectReader.readValue(reader);
            LOG.debug("Timeline metrics response => " + timelineMetrics);

            Set<String> patterns = createPatterns(metrics.keySet());

            for (TimelineMetric metric : timelineMetrics.getMetrics()) {
              if (metric.getMetricName() != null && metric.getMetricValues() != null
                  && checkMetricName(patterns, metric.getMetricName())) {
                populateResource(resource, metric);
              }
            }

          } catch (IOException io) {
            LOG.warn("Error getting timeline metrics.", io);
          } finally {
            if (reader != null) {
              try {
                reader.close();
              } catch (IOException e) {
                if (LOG.isWarnEnabled()) {
                  LOG.warn("Unable to close http input steam : spec=" + spec, e);
                }
              }
            }
          }
        }
      }

      return Collections.emptySet();
    }

    private Set<String> createPatterns(Set<String> rawNames) {
      Pattern pattern = Pattern.compile(METRIC_REGEXP_PATTERN);
      Set<String> result = new HashSet<String>();
      for (String rawName : rawNames) {
        Matcher matcher = pattern.matcher(rawName);
        StringBuilder sb = new StringBuilder();
        int lastPos = 0;
        while (matcher.find()) {
          sb.append(Pattern.quote(rawName.substring(lastPos, matcher.start())));
          sb.append(matcher.group());
          lastPos = matcher.end();
        }
        sb.append(Pattern.quote(rawName.substring(lastPos)));
        result.add(sb.toString());
      }
      return result;
    }

    private boolean checkMetricName(Set<String> patterns, String name) {
      for (String pattern : patterns) {
        if (Pattern.matches(pattern, name)) {
          return true;
        }
      }
      return false;
    }

    private Set<String> processRegexps(Set<String> metricNames) {
      Set<String> result = new HashSet<String>();
      for (String name : metricNames) {
        result.add(name.replaceAll(METRIC_REGEXP_PATTERN, Matcher.quoteReplacement("%")));
      }
      return result;
    }

    private void populateResource(Resource resource, TimelineMetric metric) {
      String metric_name = metric.getMetricName();
      Set<String> propertyIdSet = metrics.get(metric_name);
      List<String> parameterList  = new LinkedList<String>();

      if (propertyIdSet == null) {
        for (Map.Entry<String, Set<String>> entry : metrics.entrySet()) {
          String key = entry.getKey();
          Pattern pattern = Pattern.compile(key);
          Matcher matcher = pattern.matcher(metric_name);

          if (matcher.matches()) {
            propertyIdSet = entry.getValue();
            // get parameters
            for (int i = 0; i < matcher.groupCount(); ++i) {
              parameterList.add(matcher.group(i + 1));
            }
            break;
          }
        }
      }
      if (propertyIdSet != null) {
        Map<String, PropertyInfo> metricsMap = getComponentMetrics().get(getOverridenComponentName(resource));
        if (metricsMap != null) {
          for (String propertyId : propertyIdSet) {
            if (propertyId != null) {
              if (metricsMap.containsKey(propertyId)){
                if (containsArguments(propertyId)) {
                  int i = 1;
                  for (String param : parameterList) {
                    propertyId = substituteArgument(propertyId, "$" + i, param);
                    ++i;
                  }
                }
                Object value = getValue(metric, temporalInfo != null);
                if (value != null) {
                  resource.setProperty(propertyId, value);
                }
              }
            }
          }
        }
      }
    }
  }

  @Override
  public Set<Resource> populateResourcesWithProperties(Set<Resource> resources,
               Request request, Set<String> propertyIds) throws SystemException {

    Map<String, Map<TemporalInfo, MetricsRequest>> requestMap =
      getMetricsRequests(resources, request, propertyIds);

    // For each cluster
    for (Map.Entry<String, Map<TemporalInfo, MetricsRequest>> clusterEntry : requestMap.entrySet()) {
      // For each request
      for (MetricsRequest metricsRequest : clusterEntry.getValue().values() ) {
        metricsRequest.populateResources();
      }
    }

    return resources;
  }

  private Map<String, Map<TemporalInfo, MetricsRequest>> getMetricsRequests(
              Set<Resource> resources, Request request, Set<String> ids) throws SystemException {

    Map<String, Map<TemporalInfo, MetricsRequest>> requestMap =
      new HashMap<String, Map<TemporalInfo, MetricsRequest>>();

    String collectorHostName = null;
    String collectorPort = null;

    for (Resource resource : resources) {
      String clusterName = (String) resource.getPropertyValue(clusterNamePropertyId);
      Map<TemporalInfo, MetricsRequest> requests = requestMap.get(clusterName);
      if (requests == null) {
        requests = new HashMap<TemporalInfo, MetricsRequest>();
        requestMap.put(clusterName, requests);
      }

      if (collectorHostName == null) {
        collectorHostName = hostProvider.getCollectorHostName(clusterName, TIMELINE_METRICS);
      }

      if (collectorPort == null) {
        collectorPort = hostProvider.getCollectorPortName(clusterName, TIMELINE_METRICS);
      }

      for (String id : ids) {
        Map<String, PropertyInfo> propertyInfoMap = new HashMap<String, PropertyInfo>();

        String componentName = getOverridenComponentName(resource);

        Map<String, PropertyInfo> componentMetricMap = getComponentMetrics().get(componentName);

        // Not all components have metrics
        if (componentMetricMap != null &&
            !componentMetricMap.containsKey(id)) {
          updateComponentMetricMap(componentMetricMap, id);
        }

        getPropertyInfoMap(componentName, id, propertyInfoMap);

        for (Map.Entry<String, PropertyInfo> entry : propertyInfoMap.entrySet()) {
          String propertyId = entry.getKey();
          PropertyInfo propertyInfo = entry.getValue();

          TemporalInfo temporalInfo = request.getTemporalInfo(id);

          if ((temporalInfo == null && propertyInfo.isPointInTime()) ||
            (temporalInfo != null && propertyInfo.isTemporal())) {

            MetricsRequest metricsRequest = requests.get(temporalInfo);
            if (metricsRequest == null) {
              metricsRequest = new MetricsRequest(temporalInfo,
                getAMSUriBuilder(collectorHostName,
                  collectorPort != null ? Integer.parseInt(collectorPort) : 8188));
              requests.put(temporalInfo, metricsRequest);
            }
            metricsRequest.putResource(getHostName(resource), resource);
            metricsRequest.putPropertyId(propertyInfo.getPropertyId(), propertyId);
          }
        }
      }
    }

    return requestMap;
  }

  static URIBuilder getAMSUriBuilder(String hostname, int port) {
    URIBuilder uriBuilder = new URIBuilder();
    uriBuilder.setScheme("http");
    uriBuilder.setHost(hostname);
    uriBuilder.setPort(port);
    uriBuilder.setPath("/ws/v1/timeline/metrics");
    return uriBuilder;
  }
}
