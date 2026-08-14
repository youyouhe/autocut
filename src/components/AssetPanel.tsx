import { useState, useEffect, useRef } from 'react';
import { Upload, Play, Info, FileVideo, Image as ImageIcon, Music, RefreshCw } from 'lucide-react';

type Asset = {
  name: string;
  asset_type: string;
  path: string;
  size: number;
  modified_at: string;
  entry_type: string;
};

export default function AssetPanel() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fetchAssets = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/assets');
      const data = await res.json();
      setAssets(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAssets();
  }, []);

  const handleUploadClick = () => fileInputRef.current?.click();

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files?.length) return;
    setUploading(true);
    const formData = new FormData();
    for (let i = 0; i < e.target.files.length; i++) {
      formData.append('files', e.target.files[i]);
    }

    try {
      await fetch('/api/upload', {
        method: 'POST',
        body: formData,
      });
      await fetchAssets();
    } catch (err) {
      console.error(err);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const formatSize = (bytes: number) => {
    return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  };

  const getIcon = (type: string) => {
    if (type === 'video') return <FileVideo size={32} strokeWidth={1} className="text-[#121212]/40" />;
    if (type === 'image') return <ImageIcon size={32} strokeWidth={1} className="text-[#121212]/40" />;
    if (type === 'audio') return <Music size={32} strokeWidth={1} className="text-[#121212]/40" />;
    return <FileVideo size={32} strokeWidth={1} className="text-[#121212]/40" />;
  };

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto">
      <div className="flex items-center justify-between mb-12 border-b border-[#121212]/10 pb-6">
        <h2 className="text-4xl font-light italic font-serif">Digital Assets</h2>
        <div className="flex gap-4">
          <button
            onClick={fetchAssets}
            className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold"
          >
            <RefreshCw size={14} strokeWidth={1.5} />
            Refresh
          </button>
          <button
            onClick={handleUploadClick}
            disabled={uploading}
            className="flex items-center gap-2 px-4 py-2 bg-[#121212] hover:bg-[#121212]/80 text-white transition-colors disabled:opacity-50 text-[10px] uppercase tracking-widest font-bold"
          >
            <Upload size={14} strokeWidth={1.5} />
            {uploading ? 'Uploading...' : 'Upload'}
          </button>
          <input
            type="file"
            multiple
            className="hidden"
            ref={fileInputRef}
            onChange={handleFileChange}
          />
        </div>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[#121212]"></div>
        </div>
      ) : assets.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-[#121212]/50">
          <FileVideo size={48} className="mb-4 opacity-20" strokeWidth={1} />
          <p className="font-serif italic text-2xl">No assets found</p>
          <p className="text-[10px] uppercase tracking-widest mt-2">Upload files to initialize</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-8">
          {assets.map((asset, idx) => (
            <div key={idx} className="border border-[#121212]/10 overflow-hidden group flex flex-col">
              <div className="h-48 border-b border-[#121212]/10 flex items-center justify-center relative bg-[#121212]/5">
                {getIcon(asset.asset_type)}
                {asset.asset_type === 'video' && (
                  <div className="absolute inset-0 bg-[#FDFCF8]/80 opacity-0 group-hover:opacity-100 flex items-center justify-center transition-opacity cursor-pointer">
                    <div className="border border-[#121212] p-4 text-[#121212] hover:bg-[#121212] hover:text-[#FDFCF8] transition-colors">
                      <Play size={20} strokeWidth={1.5} className="ml-1" />
                    </div>
                  </div>
                )}
              </div>
              <div className="p-6 flex-1 flex flex-col">
                <h3 className="font-serif text-lg italic truncate mb-2" title={asset.name}>{asset.name}</h3>
                <div className="flex items-center justify-between text-[10px] uppercase tracking-widest opacity-50 mb-6">
                  <span>{asset.asset_type}</span>
                  <span>{formatSize(asset.size)}</span>
                </div>
                <div className="flex gap-2 mt-auto">
                  <button className="flex-1 px-3 py-2 border border-[#121212]/20 text-[#121212] text-[10px] uppercase tracking-widest font-bold hover:bg-[#121212]/5 transition-colors">
                    Analyze
                  </button>
                  <button className="px-3 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors" title="Details">
                    <Info size={14} strokeWidth={1.5} />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
