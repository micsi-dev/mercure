/*
 * micsi-queue-overlay.js — MICSI additions to the mercure queue page.
 * ==================================================================
 *
 * Loaded after upstream's queue.html has initialised. Nothing here edits an
 * upstream file, so syncing with mercure-imaging/mercure will not conflict.
 *
 * What it adds back after the 2026-09 upstream sync:
 *
 *   1. A grouping selector on the Archive tab (Grouped / Patients / Studies /
 *      Series). Upstream only offers a binary study filter. Selecting a
 *      grouping sends group_by to /api/find-tasks-micsi, which is served by
 *      bookkeeping/micsi_query.py.
 *
 *   2. The wider archive table: UID and Files columns, and an expander that
 *      lists a patient's or study's child tasks.
 *
 *   3. The advanced DICOM viewer, whose backend (api.py: /task-dicom-*) and
 *      script both survived the sync but lost their entry point when
 *      upstream's queue.html replaced ours.
 *
 * It replaces upstream's #jobs_archive table rather than extending it, because
 * the two use different column sets and different endpoints. Upstream's table
 * and its /api/find-tasks endpoint are left untouched for every other tab.
 */

(function () {
  "use strict";

  function escapeHtml(s) {
    return $("<div>").text(s == null ? "" : s).html();
  }

  // --- 1. grouping selector -------------------------------------------------
  function injectGroupingSelector() {
    if ($("#archive_group_by").length) return;           // already present
    var $search = $("#job_archive_search").closest(".field");
    if (!$search.length) return;                          // upstream markup changed
    $search.prepend(
      '<div class="control">' +
        '<div class="select">' +
          '<select id="archive_group_by">' +
            '<option value="">Grouped</option>' +
            '<option value="patient">Patients</option>' +
            '<option value="study">Studies</option>' +
            '<option value="series">Series</option>' +
          '</select>' +
        '</div>' +
      '</div>'
    );
    $("#job_archive_search").attr(
      "placeholder", "Find job by ACC, MRN, or patient name");
    $("#archive_group_by").on("change", function () {
      if ($.fn.DataTable.isDataTable("#jobs_archive")) {
        $("#jobs_archive").DataTable().ajax.reload();
      }
    });
  }

  // Every archive toolbar button acts on the selected row. Reading
  // .data()[0].task_id with nothing selected throws, which is what happened
  // while the enable/disable binding below was broken and the buttons stayed
  // live. Returning null lets each action bail quietly instead.
  function selectedArchiveTaskId() {
    var rows = $('#jobs_archive').DataTable().rows({ selected: true }).data();
    if (!rows || rows.length === 0 || !rows[0]) return null;
    return rows[0].task_id;
  }

  // --- 2. widen the archive table to the MICSI column set -------------------
  // 9 columns, matching the `columns` array below. The first is the dt-control
  // expander and carries no header text.
  var MICSI_ARCHIVE_HEAD =
    "<tr><th></th><th>ACC</th><th>MRN</th><th>UID</th>" +
    "<th>Scope</th><th>Rule</th><th>Time</th><th>Files</th><th>ID</th></tr>";

  function rebuildArchiveTable() {
    var $t = $("#jobs_archive");
    if (!$t.length) return;
    if ($t.data("micsiOverlayApplied")) return;
    if ($.fn.DataTable.isDataTable("#jobs_archive")) {
      // Upstream initialises this table with serverSide ajax, so a request to
      // /api/find-tasks is already in flight by the time we run. Destroying the
      // table does not cancel it: the response still lands, and its success
      // handler draws using the old settings -- six aoColumns entries -- against
      // the nine-column header installed below. That reads past the end of the
      // array and throws on every page load:
      //
      //   TypeError: undefined is not an object (evaluating 'a.aoColumns[Y].sWidth')
      //
      // Abort it first so the stale callback never runs.
      var oldApi = $("#jobs_archive").DataTable();
      var oldSettings = oldApi.settings()[0];
      if (oldSettings && oldSettings.jqXHR && oldSettings.jqXHR.abort) {
        oldSettings.jqXHR.abort();
      }
      oldApi.destroy();
    }
    $t.find("thead").html(MICSI_ARCHIVE_HEAD);
    $t.find("tbody").empty();
    $t.data("micsiOverlayApplied", true);

    $('#jobs_archive').DataTable( {
                "columnDefs": [
                    // Apply text escaping to data columns only (not control or special columns)
                    { targets: [1, 2, 4, 5, 6, 8], render: DataTable.render.text() },
                    {
                        targets: 0,
                        orderable: false,
                        className: 'dt-control',
                        data: null,
                        defaultContent: '',
                        render: function(data, type, row) {
                            if (row.child_count > 0) {
                                return '';
                            }
                            return '<span class="no-children">-</span>';
                        }
                    },
                    {
                        // UID column - show study_uid or series_uid based on scope
                        targets: 3,
                        data: null,
                        render: function(data, type, row) {
                            // Helper to format UIDs - show full UID(s) on separate lines
                            function formatUids(uidStr) {
                                if (!uidStr) return '-';
                                // Split by comma if multiple UIDs, show each on its own line
                                var uids = uidStr.split(', ');
                                return '<small style="white-space: pre-line; word-break: break-all;">' +
                                       uids.map(function(u) { return escapeHtml(u.trim()); }).join('\n') +
                                       '</small>';
                            }
    
                            if (row.Scope === 'PATIENT') {
                                // For patient scope, show study UID(s) only - series UIDs shown in dropdown
                                return formatUids(row.study_uid);
                            } else if (row.Scope === 'STUDY') {
                                // For study scope, show study UID only - series UIDs shown in dropdown
                                return formatUids(row.study_uid);
                            } else if (row.Scope === 'SERIES') {
                                return formatUids(row.series_uid);
                            }
                            return '-';
                        }
                    },
                    {
                        targets: 7,
                        orderable: false,
                        className: 'file-status',
                        data: 'task_id',
                        render: function(data, type, row) {
                            return '<span class="file-status-icon" data-task-id="' + data + '"><i class="fas fa-spinner fa-pulse"></i></span>';
                        }
                    }
                ],
                paging:   true,
                ordering: true,
                searching: true,
                select: true,
                language: {
                    emptyTable: "No jobs found",
                    paginate: {
                        first:      "First",
                        last:       "Last",
                        next:       "&gt;",
                        previous:   "&lt;"
                    },
                },
                ajax: {
                    url: '/api/find-tasks-micsi',
                    data: function(d) {
                        // Add the group_by parameter to the request
                        d.group_by = $('#archive_group_by').val();
                    }
                },
                columns: [
                    { data: null, defaultContent: '' },
                    { data: 'ACC' },
                    { data: 'MRN' },
                    { data: null, defaultContent: '' },  // UID (rendered via columnDefs)
                    { data: 'Scope' },
                    { data: 'Rule' },
                    { data: 'Time' },
                    { data: 'task_id' },  // Files column
                    { data: 'task_id' },  // ID column
                ],
                serverSide: true,
                autoWidth: false,
                buttons: [
                    {
                        text: '<i class="fas fa-code"></i>',
                        titleAttr: 'Job information',
                        action: function ( e, dt, node, config ) {
                            var jid = selectedArchiveTaskId();
                            if (jid === null) { return; }
                            showArchiveJobInformation(jid);
                        }
                    },
                    {
                        text: '<i class="fas fa-list-ul"></i>',
                        titleAttr: 'Audit trail',
                        action: function ( e, dt, node, config ) {
                            var jid = selectedArchiveTaskId();
                            if (jid === null) { return; }
                            showAuditTrail(jid);
                        }
                    },
                    {
                        text: '<i class="fas fa-receipt"></i>',
                        titleAttr: 'Processing log',
                        action: function ( e, dt, node, config ) {
                            var jid = selectedArchiveTaskId();
                            if (jid === null) { return; }
                            showLogs(jid);
                        }
                    },
                    {
                        text: '<i class="fas fa-chart-bar"></i>',
                        titleAttr: 'Processing results',
                        action: function ( e, dt, node, config ) {
                            var jid = selectedArchiveTaskId();
                            if (jid === null) { return; }
                            showResults(jid);
                        }
                    },
                    {
                        text: '<i class="fas fa-eye"></i>',
                        titleAttr: 'Preview output',
                        action: function ( e, dt, node, config ) {
                            var jid = selectedArchiveTaskId();
                            if (jid === null) { return; }
                            console.log('Archive preview clicked for task:', jid);
                            // First check for actual task ID (series/study files may be in parent folder)
                            $.ajax({
                                url: '/api/task-files-exist/' + jid,
                                success: function(data) {
                                    console.log('task-files-exist response:', data);
                                    if (data.exists) {
                                        var previewTaskId = data.actual_task_id || jid;
                                        console.log('Opening preview for task:', previewTaskId);
                                        checkAndOpenPreview(previewTaskId);
                                    } else {
                                        console.log('No files found, exists=false');
                                        alert('No output files found for this task.');
                                    }
                                },
                                error: function(xhr, status, error) {
                                    console.error('task-files-exist error:', status, error, xhr.responseText);
                                    alert('Could not check task files.');
                                }
                            });
                        }
                    },
                    {
                        text: '<i class="fas fa-trash-alt"></i>',
                        titleAttr: 'Delete from archive',
                        action: function ( e, dt, node, config ) {
                            var jid = selectedArchiveTaskId();
                            if (jid === null) { return; }
                            deleteArchiveJob(jid);
                        }
                    }
                ],
                dom: '<"columns"<"column"B>><"columns"<"column queuetablepadding"rt>><"columns"<"column"i><"column"p><"column"f>>',
                "scrollY": "740px",
                "scrollX": false,
                "scrollCollapse": true,
                "order": [[6, 'desc'], [8, 'desc']],
                initComplete: function() {
                    $('.queueArchiveFilter > .dt-down-arrow').hide()
                    $('.queueArchiveFilter > .icon').hide()
                },
                drawCallback: function() {
                    // Check file status for visible rows
                    checkFileStatusForVisibleRows();
                }
            } );

    $('#jobs_archive tbody').on('click', 'td.dt-control', function () {
                var tr = $(this).closest('tr');
                var row = $('#jobs_archive').DataTable().row(tr);
                var rowData = row.data();
    
                // Only toggle if this row has children
                if (!rowData || rowData.child_count <= 0) {
                    return;
                }
    
                if (row.child.isShown()) {
                    // Close this row
                    row.child.hide();
                    tr.removeClass('shown');
                } else {
                    // Open this row - fetch child tasks
                    tr.addClass('shown');
                    row.child('<div class="has-text-centered"><i class="fas fa-spinner fa-pulse"></i> Loading...</div>').show();
    
                    $.ajax({
                        url: '/api/get-child-tasks',
                        data: {
                            parent_id: rowData.task_id,
                            scope: rowData.Scope.toLowerCase()
                        },
                        success: function(data) {
                            row.child(formatChildTasks(data, rowData.Scope)).show();
                        },
                        error: function() {
                            row.child('<div class="has-text-danger">Failed to load child tasks</div>').show();
                        }
                    });
                }
            });

    $('#jobs_archive tbody').on('click', '.child-info-btn', function (e) {
                e.stopPropagation();
                var taskId = $(this).closest('tr').data('task-id');
                if (taskId) showArchiveJobInformation(taskId);
            });

    $('#jobs_archive tbody').on('click', '.child-audit-btn', function (e) {
                e.stopPropagation();
                var taskId = $(this).closest('tr').data('task-id');
                if (taskId) showAuditTrail(taskId);
            });

    $('#jobs_archive tbody').on('click', '.child-preview-btn', function (e) {
                e.stopPropagation();
                var taskId = $(this).closest('tr').data('task-id');
                console.log('Child preview clicked for task:', taskId);
                if (taskId) {
                    // First check for actual task ID (series files may be in parent folder)
                    $.ajax({
                        url: '/api/task-files-exist/' + taskId,
                        success: function(data) {
                            console.log('task-files-exist response for child:', data);
                            if (data.exists) {
                                var previewTaskId = data.actual_task_id || taskId;
                                console.log('Opening preview for child task:', previewTaskId);
                                checkAndOpenPreview(previewTaskId);
                            } else {
                                console.log('No files found for child, exists=false');
                                alert('No output files found for this task.');
                            }
                        },
                        error: function(xhr, status, error) {
                            console.error('task-files-exist error for child:', status, error, xhr.responseText);
                            alert('Could not check task files.');
                        }
                    });
                }
            });
  }

  // --- archive row actions ------------------------------------------------
  // Defined in our old queue.html; upstream has no equivalent. checkAndOpenPreview
  // is the entry point to the advanced DICOM viewer.
  function checkAndOpenPreview(taskId) {
          $.ajax({
              url: '/api/task-dicom-files/' + taskId,
              success: function(data) {
                  // Debug logging
                  console.log('Preview API response:', data);
                  console.log('Series count:', data.series_count);
                  console.log('Series:', data.series);
  
                  // Get all series (including PDFs now)
                  var allSeries = data.series || [];
  
                  console.log('All series:', allSeries.length, allSeries);
  
                  if (allSeries.length > 1) {
                      // Multiple series (images and/or PDFs) - show series selector
                      console.log('Showing series selector for', allSeries.length, 'series');
                      showSeriesSelector(taskId, allSeries, data.files);
                  } else if (allSeries.length === 1) {
                      // Single series - check if it's PDF or image
                      var series = allSeries[0];
                      if (series.is_pdf) {
                          var pdfFile = data.files.find(function(f) { return f.is_pdf; });
                          if (pdfFile) {
                              console.log('Opening PDF viewer for single PDF series');
                              openPdfViewer(taskId, pdfFile.filename);
                          }
                      } else {
                          console.log('Opening single image series directly:', series.series_uid);
                          openAdvancedDicomViewer(taskId, series.series_uid);
                      }
                  } else if (data.files.length > 0) {
                      // Fallback: has files but no series info
                      console.log('Fallback: opening viewer without series filter');
                      openAdvancedDicomViewer(taskId);
                  } else {
                      alert('No DICOM files found in output folder.');
                  }
              },
              error: function(xhr) {
                  if (xhr.status === 404) {
                      alert('Task output folder not found. The files may have been cleaned up or the task ID does not match a folder on disk.');
                  } else if (xhr.status === 403) {
                      alert('Access denied. Please log in again.');
                  } else {
                      alert('Could not access task output files: ' + (xhr.responseJSON ? xhr.responseJSON.error : 'unknown error'));
                  }
              }
          });
      }

  function deleteArchiveJob(taskId) {
          if (taskId === "") {
              return;
          }
  
          if (!confirm("Are you sure you want to delete this task from the archive? This will remove the database records but not any files on disk.")) {
              return;
          }
  
          $.ajax({
              type: 'DELETE',
              url: '/queue/jobs/archive/' + taskId,
              dataType: 'json',
              error: function (xhr) {
                  if (xhr.responseJSON && xhr.responseJSON.hasOwnProperty("error")) {
                      alert("Delete failed: " + xhr.responseJSON["error"]);
                  } else {
                      alert("Delete failed: unknown error.");
                  }
              },
              success: function (data) {
                  if (data.hasOwnProperty("error")) {
                      alert("Delete failed: " + data["error"]);
                  } else {
                      alert("Task deleted from archive successfully.");
                      $('#jobs_archive').DataTable().ajax.reload();
                  }
              },
              timeout: 5000
          });
      }

  function openPdfViewer(taskId, filename) {
          $('#pdf_frame').attr('src', '/api/task-pdf/' + taskId + '/' + filename);
          $('#pdf_viewer_modal').addClass('is-active');
      }

  // Clears the frame as well as hiding the modal, so the embedded PDF stops
  // rendering instead of sitting behind the overlay.
  function closePdfViewer() {
          $('#pdf_viewer_modal').removeClass('is-active');
          $('#pdf_frame').attr('src', '');
      }

  // --- file status --------------------------------------------------------
  // Called from the table's drawCallback. Without it every draw throws and the
  // Files column keeps spinning.
  function checkFileStatusForVisibleRows() {
          $('#jobs_archive tbody .file-status-icon').each(function() {
              var $icon = $(this);
              var taskId = $icon.data('task-id');
  
              // Skip if already checked (not showing spinner)
              if (!$icon.find('.fa-spinner').length) {
                  return;
              }
  
              $.ajax({
                  url: '/api/task-files-exist/' + taskId,
                  success: function(data) {
                      var html;
                      // Use actual_task_id for preview/navigation (may differ for series tasks)
                      var actualTaskId = data.actual_task_id || taskId;
                      if (data.exists) {
                          if (data.location === 'success') {
                              html = '<a href="#" class="file-link has-text-success" data-tab="success" data-task-id="' + actualTaskId + '">Success</a>';
                          } else if (data.location === 'error') {
                              html = '<a href="#" class="file-link has-text-danger" data-tab="fail" data-task-id="' + actualTaskId + '">Error</a>';
                          } else {
                              html = '<span class="has-text-grey">On Disk</span>';
                          }
                      } else {
                          html = '<span class="has-text-grey-light">-</span>';
                      }
                      $icon.html(html);
                  },
                  error: function() {
                      $icon.html('<span class="has-text-grey-light">?</span>');
                  }
              });
          });
      }

  // --- child task rendering -----------------------------------------------
  function formatChildTasks(data, parentScope) {
          if (!data || data.length === 0) {
              return '<div class="has-text-grey-light" style="padding: 10px;">No child tasks found</div>';
          }
  
          var html = '<table class="table is-narrow child-tasks-table is-hoverable">';
          html += '<thead><tr>';
  
          if (parentScope === 'PATIENT') {
              // For patient parents, show study/series info
              html += '<th>Scope</th>';
              html += '<th>UID</th>';
              html += '<th>Series Description</th>';
              html += '<th>Modality</th>';
              html += '<th>Rule</th>';
              html += '<th>Task ID</th>';
              html += '<th>Actions</th>';
          } else {
              // For study parents, show series info
              html += '<th>Series UID</th>';
              html += '<th>Description</th>';
              html += '<th>Modality</th>';
              html += '<th>Rule</th>';
              html += '<th>Task ID</th>';
              html += '<th>Actions</th>';
          }
          html += '</tr></thead><tbody>';
  
          for (var i = 0; i < data.length; i++) {
              var child = data[i];
              var taskId = escapeHtml(child.task_id || '');
              html += '<tr class="child-task-row" data-task-id="' + taskId + '">';
  
              if (parentScope === 'PATIENT') {
                  var scopeDisplay = (child.scope || '').toUpperCase() || 'SERIES';
                  // For patient children: show study UID for studies, series UID for series
                  var displayUid = (scopeDisplay === 'STUDY') ? (child.study_uid || '-') : (child.series_uid || '-');
                  html += '<td><span class="tag is-small ' + (scopeDisplay === 'STUDY' ? 'is-info' : 'is-light') + '">' + scopeDisplay + '</span></td>';
                  html += '<td><small style="word-break: break-all; max-width: 280px; display: inline-block;">' + escapeHtml(displayUid) + '</small></td>';
                  html += '<td>' + escapeHtml(child.series_description || '') + '</td>';
                  html += '<td>' + escapeHtml(child.modality || '') + '</td>';
                  html += '<td>' + escapeHtml(child.rule || '') + '</td>';
                  html += '<td><small>' + taskId + '</small></td>';
              } else {
                  // For study children: show full series UID
                  html += '<td><small style="word-break: break-all; max-width: 280px; display: inline-block;">' + escapeHtml(child.series_uid || '-') + '</small></td>';
                  html += '<td>' + escapeHtml(child.series_description || '') + '</td>';
                  html += '<td>' + escapeHtml(child.modality || '') + '</td>';
                  html += '<td>' + escapeHtml(child.rule || '') + '</td>';
                  html += '<td><small>' + taskId + '</small></td>';
              }
              // Action buttons for child tasks
              html += '<td class="child-actions">';
              html += '<button class="button is-small child-info-btn" title="Job information"><i class="fas fa-code"></i></button> ';
              html += '<button class="button is-small child-audit-btn" title="Audit trail"><i class="fas fa-list-ul"></i></button> ';
              html += '<button class="button is-small child-preview-btn" title="Preview"><i class="fas fa-eye"></i></button>';
              html += '</td>';
              html += '</tr>';
          }
  
          html += '</tbody></table>';
          return html;
      }

  // --- modal controls -------------------------------------------------------
  // The close buttons live in templates/micsi_viewer.html and the close functions
  // in advanced-dicom-viewer.js, but nothing joined them: our pre-merge queue.html
  // did that in an inline script, which did not come across with the markup.
  // Without this the viewer opens and cannot be dismissed.
  function bindViewerControls() {
    $('#close_pdf_viewer, #close_pdf_viewer_x').off('click.micsi').on('click.micsi', function () {
      closePdfViewer();
    });
    $('#close_series_selector, #close_series_selector_x').off('click.micsi').on('click.micsi', function () {
      if (window.closeSeriesSelector) window.closeSeriesSelector();
    });
    $('#close_advanced_viewer_x').off('click.micsi').on('click.micsi', function () {
      if (window.closeAdvancedDicomViewer) window.closeAdvancedDicomViewer();
    });
    // Escape closes whichever modal is open, innermost first.
    $(document).off('keydown.micsiViewer').on('keydown.micsiViewer', function (e) {
      if (e.keyCode !== 27) return;
      if ($('#pdf_viewer_modal').hasClass('is-active')) { closePdfViewer(); return; }
      if ($('#series_selector_modal').hasClass('is-active')) {
        if (window.closeSeriesSelector) window.closeSeriesSelector();
        return;
      }
      if ($('#advanced_dicom_viewer_modal').hasClass('is-active')) {
        if (window.closeAdvancedDicomViewer) window.closeAdvancedDicomViewer();
      }
    });
  }

  // Upstream binds a select handler to the archive table, but that binding dies
  // with the instance destroyed above, leaving the toolbar buttons stuck
  // disabled. Rebind against ours; every button acts on the selected row.
  function bindArchiveSelection() {
    var api = $('#jobs_archive').DataTable();
    // Same form upstream uses for its other tables (queue.html), rather than a
    // custom event namespace: this runs once, from boot, so there is nothing to
    // unbind first.
    api.on('select deselect', function () {
      var selected = api.rows({ selected: true }).count() > 0;
      api.buttons().enable(selected);
    });
    api.buttons().enable(false);
  }

  // --- boot -----------------------------------------------------------------
  $(function () {
    // Upstream initialises its own tables on ready; run after it so the
    // destroy/rebuild lands on a fully constructed table.
    setTimeout(function () {
      try {
        injectGroupingSelector();
        rebuildArchiveTable();
        bindArchiveSelection();
        bindViewerControls();
      } catch (e) {
        // Never take the page down: upstream markup may have moved.
        if (window.console) console.error("[micsi-overlay] queue overlay failed:", e);
      }
    }, 0);
  });
  // Rendered markup uses inline handlers, which resolve against window.
  window.checkAndOpenPreview = checkAndOpenPreview;
  window.deleteArchiveJob = deleteArchiveJob;
  window.openPdfViewer = openPdfViewer;
  window.closePdfViewer = closePdfViewer;
})();
