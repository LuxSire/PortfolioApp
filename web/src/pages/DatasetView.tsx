import DatasetTable from '../components/DatasetTable'

// The Dataset tab -- just the page wrapper around DatasetTable (see that
// component for everything else: fetch, Run-job state/polling, and the
// table itself). Split out so DatasetTable can be dropped into another
// page too, not just this one.
export default function DatasetView() {
  return (
    <div className="positions-page dataset-page">
      <DatasetTable />
    </div>
  )
}
