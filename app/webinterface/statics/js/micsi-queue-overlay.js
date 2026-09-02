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

  // --- 2. widen the archive table to the MICSI column set -------------------
  var MICSI_ARCHIVE_HEAD =
    "<tr><th></th><th></th><th>ACC</th><th>MRN</th><th>UID</th>" +
    "<th>Scope</th><th>Rule</th><th>Time</th><th>Files</th><th>ID</th></tr>";

  function rebuildArchiveTable() {
    var $t = $("#jobs_archive");
    if (!$t.length) return;
    if ($t.data("micsiOverlayApplied")) return;
    if ($.fn.DataTable.isDataTable("#jobs_archive")) {
      $("#jobs_archive").DataTable().destroy();
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
                            var jid = $('#jobs_archive').DataTable().rows( { selected: true } ).data()[0].task_id;
                            showArchiveJobInformation(jid);
                        }
                    },
                    {
                        text: '<i class="fas fa-list-ul"></i>',
                        titleAttr: 'Audit trail',
                        action: function ( e, dt, node, config ) {
                            var jid = $('#jobs_archive').DataTable().rows( { selected: true } ).data()[0].task_id;
                            showAuditTrail(jid);
                        }
                    },
                    {
                        text: '<i class="fas fa-receipt"></i>',
                        titleAttr: 'Processing log',
                        action: function ( e, dt, node, config ) {
                            var jid = $('#jobs_archive').DataTable().rows( { selected: true } ).data()[0].task_id;
                            showLogs(jid);
                        }
                    },
                    {
                        text: '<i class="fas fa-chart-bar"></i>',
                        titleAttr: 'Processing results',
                        action: function ( e, dt, node, config ) {
                            var jid = $('#jobs_archive').DataTable().rows( { selected: true } ).data()[0].task_id;
                            showResults(jid);
                        }
                    },
                    {
                        text: '<i class="fas fa-eye"></i>',
                        titleAttr: 'Preview output',
                        action: function ( e, dt, node, config ) {
                            var jid = $('#jobs_archive').DataTable().rows( { selected: true } ).data()[0].task_id;
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
                            var jid = $('#jobs_archive').DataTable().rows( { selected: true } ).data()[0].task_id;
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

  // --- boot -----------------------------------------------------------------
  $(function () {
    // Upstream initialises its own tables on ready; run after it so the
    // destroy/rebuild lands on a fully constructed table.
    setTimeout(function () {
      try {
        injectGroupingSelector();
        rebuildArchiveTable();
      } catch (e) {
        // Never take the page down: upstream markup may have moved.
        if (window.console) console.error("[micsi-overlay] queue overlay failed:", e);
      }
    }, 0);
  });
})();
