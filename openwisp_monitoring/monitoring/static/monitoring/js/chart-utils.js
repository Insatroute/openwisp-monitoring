"use strict";
const isChartZoomed = "ow2-chart-custom-zoom"; // true/false
const isChartZoomScroll = "ow2-chart-custom-zoom-scroll"; // true/false
const isCustomDateRange = "ow2-chart-custom-daterange"; // true/false
const timeRangeKey = "ow2-chart-time-range"; // 30d
const startDayKey = "ow2-chart-start-day"; // September 3, 2022
const endDayKey = "ow2-chart-end-day"; // October 3, 2022
const startDateTimeKey = "ow2-chart-start-datetime"; // 2022-09-03 00:00:00
const endDateTimeKey = "ow2-chart-end-datetime"; // 2022-09-03 00:00:00
const pickerChosenLabelKey = "ow2-chart-picker-label"; // 2022-09-03 00:00:00
const zoomtimeRangeKey = "ow2-chart-zoom-time-range"; // 30d
const zoomStartDayKey = "ow2-chart-zoom-start-day"; // September 3, 2022
const zoomEndDayKey = "ow2-chart-zoom-end-day"; // October 3, 2022
const zoomStartDateTimeKey = "ow2-chart-zoom-start-datetime"; // 2022-09-03 00:00:00
const zoomEndDateTimeKey = "ow2-chart-zoom-end-datetime"; // 2022-09-03 00:00:00
const zoomChartIdKey = "ow2-chart-zoom-id"; // true/false

django.jQuery(
  (function ($) {
    $(document).ready(function () {
      var pickerStart,
        pickerEnd,
        pickerDays,
        pickerChosenLabel,
        end = moment();
      var range =
        localStorage.getItem(timeRangeKey) ||
        $("#monitoring-timeseries-default-time").data("value");
      var start =
        localStorage.getItem(isCustomDateRange) === "true"
          ? moment()
          : moment().subtract(range.split("d")[0], "days");

      // Initialize date range picker labels
      var last1DayLabel = gettext("Last 1 Day");
      var last3DaysLabel = gettext("Last 3 Days");
      var last7DaysLabel = gettext("Last 7 Days");
      var last30DaysLabel = gettext("Last 30 Days");
      var last365DaysLabel = gettext("Last 365 Days");
      var customDateRangeLabel = gettext("Custom Range");

      // Add label to daterangepicker widget
      function addDateRangePickerLabel(startDate, endDate) {
        $("#daterangepicker-widget span").html(startDate + " - " + endDate);
      }

      function initDateRangePickerWidget(start, end) {
        addDateRangePickerLabel(
          start.format("MMMM D, YYYY"),
          end.format("MMMM D, YYYY"),
        );
        $(`[data-range-key='${last1DayLabel}']`).attr("data-time", "1d");
        $(`[data-range-key='${last3DaysLabel}']`).attr("data-time", "3d");
        $(`[data-range-key='${last7DaysLabel}']`).attr("data-time", "7d");
        $(`[data-range-key='${last30DaysLabel}']`).attr("data-time", "30d");
        $(`[data-range-key='${last365DaysLabel}']`).attr("data-time", "365d");
        $(`[data-range-key='${customDateRangeLabel}']`).attr(
          "data-time",
          customDateRangeLabel,
        );
      }

      $("#daterangepicker-widget").daterangepicker(
        {
          startDate: start,
          endDate: end,
          maxDate: moment(),
          maxSpan: {
            year: 1,
          },
          locale: {
            applyLabel: gettext("Apply"),
            cancelLabel: gettext("Cancel"),
            customRangeLabel: gettext(customDateRangeLabel),
          },
          ranges: {
            [`${last1DayLabel}`]: [moment().subtract(1, "days"), moment()],
            [`${last3DaysLabel}`]: [moment().subtract(3, "days"), moment()],
            [`${last7DaysLabel}`]: [moment().subtract(7, "days"), moment()],
            [`${last30DaysLabel}`]: [moment().subtract(30, "days"), moment()],
            [`${last365DaysLabel}`]: [moment().subtract(365, "days"), moment()],
          },
        },
        initDateRangePickerWidget,
      );
      initDateRangePickerWidget(start, end);

      function setDateRangePickerWidget(pickerStartDate, pickerEndDate) {
        addDateRangePickerLabel(
          pickerStartDate.format("MMMM D, YYYY"),
          pickerEndDate.format("MMMM D, YYYY"),
        );
        $("#daterangepicker-widget")
          .data("daterangepicker")
          .setStartDate(
            moment(pickerStartDate.format("MMMM D, YYYY")).format("MM/DD/YYYY"),
          );
        $("#daterangepicker-widget")
          .data("daterangepicker")
          .setEndDate(
            moment(pickerEndDate.format("MMMM D, YYYY")).format("MM/DD/YYYY"),
          );
      }
      function isMonitoringChartsLocation() {
        // If active monitoring charts location is not #ow-chart-container and not admin
        return (
          window.location.hash === "#ow-chart-container" ||
          window.location.pathname === "/admin/"
        );
      }

      function handleChartZoomChange(chartsContainers) {
        // Simply return if we are not at the monitoring chart location
        if (!isMonitoringChartsLocation()) {
          return;
        }
        // Handle chart zooming with custom dates
        var zoomCharts = document.getElementsByClassName(chartsContainers);
        // Set zoomChartId, required for scrolling after the zoom-in event
        $(".js-plotly-plot").on(
          "click dblclick mouseover mouseout",
          function () {
            var zoomChartId = $(this).parent().prop("id");
            if (zoomChartId === "chart-0") {
              var activeChartsLocation = window.location.hash;
              zoomChartId =
                activeChartsLocation === "#ow-chart-container"
                  ? "container"
                  : "ow-chart-inner-container";
            }
            localStorage.setItem(zoomChartIdKey, zoomChartId);
          },
        );
        for (var zoomChart of zoomCharts) {
          if (!zoomChart) {
            localStorage.setItem(isChartZoomed, false);
            localStorage.setItem(isCustomDateRange, false);
            return;
          }
          zoomChart.on("plotly_relayout", function (eventdata) {
            // jshint ignore:line
            var eventEnd = eventdata["xaxis.range[1]"];
            var eventStart = eventdata["xaxis.range[0]"];
            // Simply return if we are not at the monitoring chart location
            if (!isMonitoringChartsLocation()) {
              return;
            }
            // When the chart is zoomed out, then load charts with initial zoom level
            if (!eventEnd && !eventStart) {
              localStorage.setItem(isChartZoomed, false);
              localStorage.setItem(isCustomDateRange, false);
              // After zoomed out, it should scroll back initial zoom container
              localStorage.setItem(isChartZoomScroll, true);
              var daysBeforeZoom = localStorage.getItem(timeRangeKey);
              // Set custom date range labels & select custom date ranges for the widget
              var initialStartLabel =
                localStorage.getItem(startDayKey) ||
                moment().format("MMMM D, YYYY");
              var initialEndLabel =
                localStorage.getItem(endDayKey) ||
                moment().format("MMMM D, YYYY");
              var initialStartDate = moment(initialStartLabel, "MMMM D, YYYY");
              var initialEndDate = moment(initialEndLabel, "MMMM D, YYYY");
              // Set date range picker widget labels
              setDateRangePickerWidget(initialStartDate, initialEndDate);
              // On zoom out, load all charts to their initial zoom level
              loadCharts(daysBeforeZoom, true);
              // refresh every 2.5 minutes
              clearInterval(window.owChartRefresh);
              window.owChartRefresh = setInterval(
                loadFetchedCharts,
                1000 * 60 * 2.5,
                daysBeforeZoom,
              );
              return;
            }
            // When the chart zoomed in,
            var pickerEndDate = moment(eventEnd);
            var pickerStartDate = moment(eventStart);
            var pickerDays = pickerEndDate.diff(pickerStartDate, "days") + "d";
            if (pickerDays === "0d") {
              pickerDays = "1d";
            }
            // Set custom date range values
            localStorage.setItem(
              zoomStartDateTimeKey,
              pickerStartDate.format("YYYY-MM-DD HH:mm:ss"),
            );
            localStorage.setItem(
              zoomEndDateTimeKey,
              pickerEndDate.format("YYYY-MM-DD HH:mm:ss"),
            );
            localStorage.setItem(
              zoomStartDayKey,
              pickerStartDate.format("MMMM D, YYYY"),
            );
            localStorage.setItem(
              zoomEndDayKey,
              pickerEndDate.format("MMMM D, YYYY"),
            );
            localStorage.setItem(isChartZoomScroll, true);
            localStorage.setItem(isChartZoomed, true);
            localStorage.setItem(isCustomDateRange, true);
            localStorage.setItem(zoomtimeRangeKey, pickerDays);
            // Set date range picker widget labels
            setDateRangePickerWidget(pickerStartDate, pickerEndDate);
            // Now, load the charts with custom date ranges
            loadCharts(pickerDays, true);
            // refresh every 2.5 minutes
            clearInterval(window.owChartRefresh);
            window.owChartRefresh = setInterval(
              loadFetchedCharts,
              1000 * 60 * 2.5,
              pickerDays,
            );
          });
        }
      }

      function triggerZoomCharts(containerClassName) {
        handleChartZoomChange(containerClassName);
        const zoomChartContainer = document.getElementById(
          localStorage.getItem(zoomChartIdKey),
        );
        // If the chart zoom scrolling is active, then scroll to the zoomed chart container
        if (
          localStorage.getItem(isChartZoomScroll) === "true" &&
          zoomChartContainer
        ) {
          zoomChartContainer.scrollIntoView();
        }
      }
      var chartQuickLinks,
        chartContents = $("#ow-chart-contents"),
        fallback = $("#ow-chart-fallback"),
        defaultTimeRange =
          localStorage.getItem(timeRangeKey) ||
          $("#monitoring-timeseries-default-time").data("value"),
        apiUrl = $("#monitoring-timeseries-api-url").data("value"),
        baseUrl = `${apiUrl}?time=`,
        globalLoadingOverlay = $("#loading-overlay"),
        localLoadingOverlay = $("#chart-loading-overlay"),
        getChartFetchUrl = function (time) {
          var url = baseUrl + time;
          // pass pickerEndDate and pickerStartDate to url
          if (
            localStorage.getItem(isCustomDateRange) === "true" ||
            localStorage.getItem(pickerChosenLabelKey) === customDateRangeLabel
          ) {
            var startDate = localStorage.getItem(startDateTimeKey);
            var endDate = localStorage.getItem(endDateTimeKey);
            if (localStorage.getItem(isChartZoomed) === "true") {
              endDate = localStorage.getItem(zoomEndDateTimeKey);
              startDate = localStorage.getItem(zoomStartDateTimeKey);
            }
            // Ensure that the 'endDate' of zooming events
            // is never greater than the 'now' date time
            const now = moment().format("YYYY-MM-DD HH:mm:ss");
            const endDateTime = moment(endDate).format("YYYY-MM-DD HH:mm:ss");
            endDate = endDateTime > now ? now : endDateTime;
            url = `${apiUrl}?start=${startDate}&end=${endDate}`;
            var timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
            if (timezone) {
              url = `${url}&timezone=${timezone}`;
            }
          }
          if ($("#org-selector").val()) {
            var orgSlug = $("#org-selector").val();
            url = `${url}&organization_slug=${orgSlug}`;
          }
          return url;
        },
        createCharts = function (data) {
          $.each(data.charts, function (i, chart) {
            var htmlId = "chart-" + i,
              chartDiv = $("#" + htmlId),
              chartQuickLink = chartQuickLinks[chart.title];
            if (!chartDiv.length) {
              chartContents.append(
                '<div id="' +
                  htmlId +
                  '" class="ow-chart">' +
                  '<div class="js-plotly-plot"></div></div>',
              );
            }
            createChart(
              chart,
              data.x,
              htmlId,
              chart.title,
              chart.type,
              chartQuickLink,
            );
          });
        },
        addOrganizationSelector = function (data) {
          var orgSelector = $("#org-selector");
          if (data.organizations === undefined) {
            return;
          }
          if (orgSelector.data("select2-id") === "org-selector") {
            return;
          }
          orgSelector.parent().show();
          orgSelector.select2({
            data: data.organizations,
            allowClear: true,
            placeholder: gettext("Organization Filter"),
          });
          orgSelector.show();
        },
        loadCharts = function (time, showLoading) {
          $.ajax(getChartFetchUrl(time), {
            dataType: "json",
            xhrFields: {
              withCredentials: true,
            },
            beforeSend: function () {
              chartContents.hide();
              chartContents.empty();
              fallback.hide();
              if (showLoading) {
                globalLoadingOverlay.show();
              }
              localLoadingOverlay.show();
            },
            success: function (data) {
              localLoadingOverlay.hide();
              if (data.charts.length) {
                chartContents.show();
              } else {
                fallback.show();
              }
              window.owLastChartsData = data;
              createCharts(data);
              addOrganizationSelector(data);
            },
            error: function (response) {
              var errorMessage = gettext(
                "Something went wrong while loading the charts",
              );
              if (response.responseJSON) {
                if (response.responseJSON.constructor === Array) {
                  errorMessage =
                    errorMessage + ": " + response.responseJSON.join(" ");
                }
              }
              alert(errorMessage);
            },
            complete: function () {
              triggerZoomCharts("js-plotly-plot");
              localLoadingOverlay.fadeOut(200, function () {
                if (showLoading) {
                  globalLoadingOverlay.fadeOut(200);
                }
              });
            },
          });
        };
      try {
        chartQuickLinks = JSON.parse($("#monitoring-chart-quick-links").html());
      } catch (error) {
        chartQuickLinks = {};
      }

      window.triggerChartLoading = function () {
        // Charts load with the last set time range or default time range
        var range = localStorage.getItem(timeRangeKey) || defaultTimeRange;
        var startLabel =
          localStorage.getItem(startDayKey) || moment().format("MMMM D, YYYY");
        var endLabel =
          localStorage.getItem(endDayKey) || moment().format("MMMM D, YYYY");

        // Disable the zoom chart and scrolling when we refresh the page
        localStorage.setItem(isChartZoomScroll, false);
        localStorage.setItem(isChartZoomed, false);

        if (localStorage.getItem(isCustomDateRange) === "true") {
          // Add label to daterangepicker widget
          addDateRangePickerLabel(startLabel, endLabel);
          // Set last selected custom date after page reload
          var startDate = moment(startLabel, "MMMM D, YYYY");
          var endDate = moment(endLabel, "MMMM D, YYYY");
          $("#daterangepicker-widget")
            .data("daterangepicker")
            .setStartDate(moment(startDate).format("MM/DD/YYYY"));
          $("#daterangepicker-widget")
            .data("daterangepicker")
            .setEndDate(moment(endDate).format("MM/DD/YYYY"));
          // Then loads charts with custom ranges selected
          loadCharts(range, true);
        } else {
          endLabel = moment().format("MMMM D, YYYY");
          startLabel = moment()
            .subtract(range.split("d")[0], "days")
            .format("MMMM D, YYYY");
          // Add label to daterangepicker widget
          addDateRangePickerLabel(startLabel, endLabel);
          // Set last selected default dates after page reload
          $(".daterangepicker .ranges ul li[data-time=" + range + "]").trigger(
            "click",
          );
        }
      };
      // try adding the browser timezone to the querystring
      try {
        var timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (timezone) {
          baseUrl = baseUrl.replace("time=", "timezone=" + timezone + "&time=");
        }
        // ignore failures (older browsers do not support this)
      } catch (e) {}

      // daterangepicker widget logic here
      $("#daterangepicker-widget").on(
        "apply.daterangepicker",
        function (ev, picker) {
          pickerChosenLabel = picker.chosenLabel;
          pickerStart = moment(picker.startDate.format("YYYY-MM-DD HH:mm:ss"));
          pickerEnd = moment(picker.endDate.format("YYYY-MM-DD HH:mm:ss"));
          pickerDays = pickerEnd.diff(pickerStart, "days") + "d";
          // set date values required for daterangepicker labels
          localStorage.setItem(pickerChosenLabelKey, pickerChosenLabel);
          localStorage.setItem(
            startDateTimeKey,
            picker.startDate.format("YYYY-MM-DD HH:mm:ss"),
          );
          localStorage.setItem(
            endDateTimeKey,
            picker.endDate.format("YYYY-MM-DD HH:mm:ss"),
          );
          localStorage.setItem(startDayKey, pickerStart.format("MMMM D, YYYY"));
          localStorage.setItem(endDayKey, pickerEnd.format("MMMM D, YYYY"));
          localStorage.setItem(isChartZoomed, false);
          localStorage.setItem(isChartZoomScroll, false);
          localStorage.setItem(timeRangeKey, pickerDays);
          localStorage.setItem(
            isCustomDateRange,
            pickerChosenLabel === "Custom Range",
          );
          loadCharts(pickerDays, true);
          // refresh charts every 2.5 minutes
          clearInterval(window.owChartRefresh);
          window.owChartRefresh = setInterval(
            loadFetchedCharts,
            1000 * 60 * 2.5,
            pickerDays,
          );
        },
      );
      // bind export button — opens column-picker modal
      $("#ow-chart-export").click(function () {
        owOpenExportPicker();
      });
      // fetch chart data and replace the old charts with the new ones
      function loadFetchedCharts(time) {
        $.ajax(getChartFetchUrl(time), {
          dataType: "json",
          success: function (data) {
            if (data.charts.length) {
              window.owLastChartsData = data;
              createCharts(data);
              triggerZoomCharts("js-plotly-plot");
            }
          },
          error: function () {
            window.console.error("Unable to fetch chart data.");
          },
        });
      }

      function owBuildExportQuery(extra) {
        var queryParams = { csv: 1 };
        queryParams.time = localStorage.getItem(timeRangeKey);
        if (
          localStorage.getItem(isCustomDateRange) === "true" ||
          localStorage.getItem(pickerChosenLabelKey) === customDateRangeLabel
        ) {
          queryParams.start = localStorage.getItem(startDateTimeKey);
          queryParams.end = localStorage.getItem(endDateTimeKey);
          if (localStorage.getItem(isChartZoomed) === "true") {
            queryParams.time = localStorage.getItem(zoomtimeRangeKey);
            queryParams.end = localStorage.getItem(zoomEndDateTimeKey);
            queryParams.start = localStorage.getItem(zoomStartDateTimeKey);
          }
          var tzExp = Intl.DateTimeFormat().resolvedOptions().timeZone;
          if (tzExp) {
            queryParams.timezone = tzExp;
          }
        }
        if ($("#org-selector").val()) {
          queryParams.organization_slug = $("#org-selector").val();
        }
        Object.keys(extra || {}).forEach(function (k) {
          queryParams[k] = extra[k];
        });
        return Object.keys(queryParams)
          .map(
            (key) =>
              `${encodeURIComponent(key)}=${encodeURIComponent(queryParams[key])}`,
          )
          .join("&");
      }

      window.owOpenExportPicker = function () {
        var data = window.owLastChartsData;
        var grid = document.getElementById("owExportGrid");
        if (!grid) return;
        if (!data || !data.charts || !data.charts.length) {
          grid.innerHTML = '<div class="ow-export-empty">' +
            gettext("No chart data available to export.") + "</div>";
          document.getElementById("owExportTotalCount").textContent = "0";
          document.getElementById("owExportSelectedCount").textContent = "0";
        } else {
          var html = "";
          var total = 0;
          data.charts.forEach(function (chart, ci) {
            var titleEsc = String(chart.title || "")
              .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
            var items = "";
            if (chart.type === "histogram") {
              // Histograms export as a key-value section, not per-trace columns.
              // Treat the whole chart as one selectable unit.
              var headerH = "__hist__:" + (chart.title || "");
              items = '<label><input type="checkbox" class="ow-export-col" data-chart-idx="' +
                ci + '" value="' + encodeURIComponent(headerH) + '" checked> ' +
                gettext("Summary") + "</label>";
              total += 1;
            } else if (Array.isArray(chart.traces)) {
              chart.traces.forEach(function (trace) {
                var traceName = trace[0];
                var traceEsc = String(traceName)
                  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
                var header = traceName + " - " + chart.title;
                items += '<label><input type="checkbox" class="ow-export-col" data-chart-idx="' +
                  ci + '" value="' + encodeURIComponent(header) + '" checked> ' +
                  traceEsc + "</label>";
                total += 1;
              });
            }
            if (!items) return;
            html += '<div class="ow-export-cat">' +
              '<div class="ow-export-cat-title">' + titleEsc + "</div>" +
              items + "</div>";
          });
          grid.innerHTML = html || '<div class="ow-export-empty">' +
            gettext("No exportable columns.") + "</div>";
          document.getElementById("owExportTotalCount").textContent = String(total);
          owUpdateExportCounter();
          $("#owExportGrid").off("change.owExport").on(
            "change.owExport", "input.ow-export-col", owUpdateExportCounter,
          );
        }
        document.getElementById("owExportOverlay").style.display = "block";
        document.getElementById("owExportModal").style.display = "flex";
      };

      window.owCloseExportPicker = function () {
        document.getElementById("owExportOverlay").style.display = "none";
        document.getElementById("owExportModal").style.display = "none";
      };

      window.owExportSelectAll = function () {
        $("#owExportGrid input.ow-export-col").prop("checked", true);
        owUpdateExportCounter();
      };

      window.owExportDeselectAll = function () {
        $("#owExportGrid input.ow-export-col").prop("checked", false);
        owUpdateExportCounter();
      };

      function owUpdateExportCounter() {
        var $boxes = $("#owExportGrid input.ow-export-col");
        var selected = $boxes.filter(":checked").length;
        document.getElementById("owExportSelectedCount").textContent = String(selected);
        var pdfBtn = document.getElementById("owExportPdfBtn");
        var pdfNote = document.getElementById("owExportPdfNote");
        var xlsxBtn = document.getElementById("owExportXlsxBtn");
        var pdfAllowed = selected > 0 && selected <= 10;
        pdfBtn.style.display = pdfAllowed ? "" : "none";
        pdfNote.style.display = selected > 10 ? "" : "none";
        xlsxBtn.disabled = selected === 0;
      }

      window.owSubmitExport = function (format) {
        var headers = $("#owExportGrid input.ow-export-col:checked")
          .map(function () { return decodeURIComponent(this.value); })
          .get();
        if (!headers.length) return;
        if (format === "pdf" && headers.length > 10) return;
        var queryString = owBuildExportQuery({
          export_format: format,
          columns: headers.join("|"),
        });
        owCloseExportPicker();
        location.href = `${apiUrl}?${queryString}`;
      };

      $("#org-selector").change(function () {
        loadCharts(
          localStorage.getItem(timeRangeKey) || defaultTimeRange,
          true,
        );
      });
    });
  })(django.jQuery),
);
