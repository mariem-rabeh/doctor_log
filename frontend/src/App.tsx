import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import DiagnosisReport from "./pages/DiagnosisReport";
import History from "./pages/History";
import InvestigationDetails from "./pages/InvestigationDetails";
import NewInvestigation from "./pages/NewInvestigation";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="new" element={<NewInvestigation />} />
        <Route path="history" element={<History />} />
        <Route path="incidents/:id" element={<InvestigationDetails />} />
        <Route path="incidents/:id/report" element={<DiagnosisReport />} />
      </Route>
    </Routes>
  );
}
